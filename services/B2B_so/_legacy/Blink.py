import imaplib
import email
from email.header import decode_header
from pypdf import PdfReader
import io
import re
import json
import openpyxl
import frappe
from difflib import SequenceMatcher
import boto3
from datetime import datetime
from khanal_tech_integrations.utils.sap import AuthenticateSAPB1
from botocore.exceptions import NoCredentialsError
from difflib import SequenceMatcher
import requests
import time
from pdf2image import convert_from_path
import pdfplumber
import os
import traceback

headersList = {
    "Accept": "*/*",
    "User-Agent": "Khanal Tech",
    "Content-Type": "application/json",
    "Prefer": "odata.maxpagesize=100"
}
payload = ''


@frappe.whitelist()
def process_email_attachments():
    # Email Configuration
    if frappe.get_single("B2B_EMAIL"):
       
        
        email_record = frappe.db.get_value(
            "B2B_EMAIL", 
            None, 
            ["EMAIL", "PASSWORD", "IMAP_SERVER", "IMAP_PORT", "LABEL_NAME"], 
            as_dict=True
        )
        
        if email_record:
            
            
            # Accessing with correct keys (lowercase)
            EMAIL = email_record.get('email')
            PASSWORD = email_record.get('password')
            IMAP_SERVER = email_record.get('imap_server')
            IMAP_PORT = email_record.get('imap_port') or 993  # Default to 993 if not set
            LABEL_NAME = email_record.get('label_name') or "Blinkit"

        else:
            # print("Email record not found in the database.")
            frappe.throw("Email record not found in the database.")
    else:
        print("Table 'B2B_EMAIL' does not exist.")
        frappe.throw("Email table does not exist.")

    # Fetch CardCode and SalesPersonCode from B2B-Customer-Details
    customer_details_record = frappe.db.get_value(
        "B2B_Customer_Details", 
        None, 
        ["cardcode1", "salespersoncode1"], 
        as_dict=True
    )
    
    if customer_details_record:
        CardCode = customer_details_record.get('cardcode1')
        SalesPersonCode = customer_details_record.get('salespersoncode1')
       
    else:
        frappe.throw("Customer details record not found in the database.")

    
    def connect_to_email():
        """Connect to the email server."""
        print("Connecting to email server...")
        try:
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            mail.login(EMAIL, PASSWORD)
            print("Connected to email server.")
            return mail
        except imaplib.IMAP4.error as e:
            frappe.throw(f"IMAP login failed: {str(e)}")
        except ConnectionRefusedError as e:
            frappe.throw(f"Connection refused: {str(e)}. Please check the IMAP server and port.")
        except Exception as e:
            frappe.throw(f"An error occurred while connecting to the email server: {str(e)}")

    def extract_pdf_text(pdf_data):
        # print("Extracting text from PDF...")
        if isinstance(pdf_data, dict):
            pdf_data = pdf_data.get("pdf", b"")  
        if not isinstance(pdf_data, bytes):
            raise TypeError(f"Expected bytes, got {type(pdf_data)}")
        try:
            pdf_file = io.BytesIO(pdf_data)
            reader = PdfReader(pdf_file)
            # print("Reader Object:", reader)
            if not reader.pages:
                # print("Warning: No pages found in the PDF.")
                return ""
            pdf_text = ''.join(page.extract_text() or '' for page in reader.pages)
            return pdf_text
        except Exception as e:
            print(f"Error extracting text: {e}")
            return ""

    def extract_text_from_pdf_with_plumber(pdf_data):
        """
        Extracts text from a PDF file.
        
        :param pdf_data: PDF file data in bytes.
        :return: Extracted text as a string.
        """
        try:
            # Save the PDF data to a temporary file
            with open("/tmp/temp_pdf.pdf", "wb") as temp_pdf_file:
                temp_pdf_file.write(pdf_data)
            
            extracted_text = ""
            with pdfplumber.open("/tmp/temp_pdf.pdf") as pdf:
                for page in pdf.pages:
                    # Extract text directly from the PDF
                    text = page.extract_text()
                    if text:
                        extracted_text += text + "\n"
            
            # Remove the temporary file
            os.remove("/tmp/temp_pdf.pdf")
            
            return extracted_text
        except Exception as e:
            print(f"Error extracting text from PDF: {e}")
            return ""

    def _normalize_ws(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "")).strip()

    def _extract_company_name(text: str) -> str:
        # Typically the company name is the first line after "Purchase Order"
        m = re.search(
            r"Purchase\s+Order\s*\r?\n\s*([A-Za-z][A-Za-z0-9 &().,\-]+)\s*\r?\n",
            text,
        )
        if m:
            return _normalize_ws(m.group(1))

        # Sometimes "Purchase Order" and company appear on the same line
        m = re.search(r"Purchase\s+Order\s+([A-Za-z][A-Za-z0-9 &().,\-]+)\s*\r?\n", text)
        if m:
            return _normalize_ws(m.group(1))

        # Common structure: Company line appears immediately before PAN
        m = re.search(r"\r?\n\s*([A-Za-z][A-Za-z0-9 &().,\-]+)\s*\r?\n\s*PAN\s*:?", text, flags=re.IGNORECASE)
        if m:
            return _normalize_ws(m.group(1))

        # Fallback: pick the first "<...> PRIVATE LIMITED" near the top of document
        top = text[:3000]
        m = re.search(r"\b([A-Za-z][A-Za-z0-9 &().,\-]+PRIVATE\s+LIMITED)\b", top, flags=re.IGNORECASE)
        if m:
            return _normalize_ws(m.group(1))

        # Fallback: known historical company names
        for candidate in ("HANDS ON TRADES PRIVATE LIMITED", "BLINK COMMERCE PRIVATE LIMITED"):
            if candidate in text:
                return candidate
        return "Not Found"

    def _extract_delivered_to(text: str) -> str:
        # Capture Delivered To block up to a known terminator.
        # Works for both "HANDS ON TRADES..." and "BLINK COMMERCE..." and future company name changes.
        m = re.search(
            r"Delivered\s*(?:\r?\n\s*)*To\s*:\s*"
            r"(?P<name>[A-Za-z][A-Za-z0-9 &().,\-]+)\s*"
            r"(?P<addr>[\s\S]*?)"
            r"(?:\r?\n\s*)?(?:GST\s*No\.?|Reference|#\s*Item)",
            text,
        )
        if not m:
            # Variant seen in PO 2173110010781:
            # Delivered :<Buyer>\nTo <Address...> ... # Item
            m = re.search(
                r"Delivered\s*:\s*(?P<name>[A-Za-z][A-Za-z0-9 &().,\-]+)\s*(?:GST\s*No\.?\s*:\s*[\w\d]+\s*)?"
                r"[\r\n]+\s*To\s*(?P<addr>[\s\S]*?)"
                r"(?:\r?\n\s*)?(?:GST\s*No\.?|Reference|#\s*Item)",
                text,
            )
            if not m:
                return "Not Found"

        name = _normalize_ws(m.group("name"))
        addr = m.group("addr") or ""
        addr = re.sub(r"\n?\s*GST No\.\s*:\s*[\w\d]+\s*", " ", addr)
        addr = re.sub(r"\n?\s*Reference\s*:.*", " ", addr)
        addr = _normalize_ws(addr)
        delivered_to = _normalize_ws(f"{name} {addr}")

        # Ensure pincode is present if available in the captured text
        pin = re.search(r"\b\d{6}\b", delivered_to)
        if not pin:
            pin2 = re.search(r"\b\d{6}\b", text)
            if pin2:
                delivered_to = _normalize_ws(f"{delivered_to} {pin2.group(0)}")
        return delivered_to

    def _extract_gst_no(text: str) -> str:
        # Prefer GST under "Delivered To" block (typically destination warehouse GST).
        delivered_to_block = re.search(
            r"Delivered\s*To\s*:\s*[\s\S]*?GST\s*No\.?\s*:\s*([0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3})",
            text,
            flags=re.IGNORECASE,
        )
        if delivered_to_block:
            return (delivered_to_block.group(1) or "").strip().upper()

        # Fallback: capture all GSTs and prefer second occurrence when present.
        gst_matches = re.findall(
            r"GST\s*No\.?\s*:\s*([0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3})",
            text,
            flags=re.IGNORECASE,
        )
        if gst_matches:
            if len(gst_matches) >= 2:
                return (gst_matches[1] or "").strip().upper()
            return (gst_matches[0] or "").strip().upper()
        return "Not Found"

    def is_valid_po_pdf_text(text: str) -> bool:
        if not text:
            return False

        # Channel guardrail:
        # Keep this generic (no hardcoded PAN/GST values).
        blink_identifiers = (
            "HANDS ON TRADES PRIVATE LIMITED",
            "BLINK COMMERCE PRIVATE LIMITED",
            "Partners Biz Portal",
        )
        has_blink_identifier = any(identifier in text for identifier in blink_identifiers)
        has_pan_pattern = re.search(r"\bPAN\s*:?\s*[A-Z]{5}\d{4}[A-Z]\b", text, flags=re.IGNORECASE) is not None
        has_gst_pattern = re.search(
            r"\bGST\s*No\.?\s*:?\s*[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}\b",
            text,
            flags=re.IGNORECASE,
        ) is not None
        is_blink = has_blink_identifier and (has_pan_pattern or has_gst_pattern)
        if not is_blink:
            return False

        has_po_number = re.search(r"P\.O\.\s*Number\s*:\s*\d+", text) is not None
        has_purchase_order = "Purchase Order" in text
        has_delivered_to = re.search(r"Delivered\s*To\s*:", text) is not None
        return bool(has_po_number and has_purchase_order and has_delivered_to)

    def extract_data(text):
        # print("Extract specific data from PDF text using regex.")
        data = {}
        contact_name_regex = r"Contact Name:\s*([A-Za-z\s]+)"
        po_number_regex = r"P\.O\.\s*Number\s*:\s*(\d+)"
        po_expiry_date_regex = r"PO expiry date\s*[:\-]?\s*([A-Za-z]{3,9}\.?[\s]\d{1,2},\s\d{4}(?:,\s*\d{1,2}:\d{2}\s*[ap]\.?m\.?)?)"

        data["company_name"] = _extract_company_name(text)

        if data["company_name"] == "HANDS ON TRADES PRIVATE LIMITED":
            data["employee_name"] = "Abhinay Pansari"
            data["employee_code"] = 103

        contact_name = re.search(contact_name_regex, text)
        data["contact_name"] = re.sub(r"Phone No.*", "", contact_name.group(1)).strip() if contact_name else "Not Found"
        data["po_number"] = re.search(po_number_regex, text).group(1) if re.search(po_number_regex, text) else "Not Found"

        po_expiry_date_match = re.search(po_expiry_date_regex, text)
        if po_expiry_date_match:
            po_expiry_date = po_expiry_date_match.group(1).replace('.', '')
            print("PO Expiry Date:", po_expiry_date)

            # Normalize month formats to "Sep"
            po_expiry_date = re.sub(r"\b(Sept|September)\b", "Sep", po_expiry_date)

            date_formats = [
                "%B %d, %Y, %I:%M %p",   # March 6, 2026, 11:59 pm
                "%b %d, %Y, %I:%M %p",   # Mar 6, 2026, 11:59 pm
                "%B %d, %Y",             # March 6, 2026
                "%b %d, %Y",             # Mar 6, 2026
                "%B %d %Y",              # March 6 2026 (no comma)
                "%b %d %Y",              # Mar 6 2026 (no comma)
            ]
            formatted_date = "Invalid Date Format"
            for date_format in date_formats:
                try:
                    datetime_object = datetime.strptime(po_expiry_date, date_format)
                    formatted_date = datetime_object.strftime("%Y-%m-%d")
                    data["po_expiry_date"] = formatted_date
                    break
                except ValueError:
                    data["po_expiry_date"] = "Invalid Date Format"
        else:
            data["po_expiry_date"] = "Not Found"

        data["delivered_to"] = _extract_delivered_to(text)
        data["gst_no"] = _extract_gst_no(text)

        return data

    def extract_data_from_plumber(text):
        # Plumber extraction is mostly used when the PDF text extraction is flaky.
        # Re-use the same robust extraction logic.
        return extract_data(text)

    def merge_extracted_data(extracted_data, extracted_data2):
        """Merge extracted_data with extracted_data2 if fields are missing."""
        for key, value in extracted_data2.items():
            if extracted_data.get(key) in [None, "Not Found"]:
                extracted_data[key] = value
        return extracted_data

    @frappe.whitelist()
    def Get_CustomerDetails(CardCode, extracted_data,extracted_data2):
        try:
            print(f"Received CardCode: {CardCode}")
            # print(f"Extracted Data: {extracted_data}")
            # print(f"Extracted Data2: {extracted_data2}")

            def similar(a, b):
                return SequenceMatcher(None, a.lower(), b.lower()).ratio()

            def normalize_for_match(s: str) -> str:
                s = (s or "").lower()
                s = re.sub(r"[^a-z0-9]+", " ", s)
                return re.sub(r"\s+", " ", s).strip()

            def normalize_gstin(value: str) -> str:
                return re.sub(r"\s+", "", (value or "")).upper().strip()

            po_number = extracted_data.get("po_number")
            delivered_to = extracted_data.get("delivered_to", "")
            extracted_gst_no = normalize_gstin(extracted_data.get("gst_no"))
            # print("Delivered To (raw):", delivered_to)

            zip_match = re.search(r'\b\d{6}\b', delivered_to)
            delivered_zip = zip_match.group() if zip_match else None
            print(f"Extracted ZIP code: {delivered_zip}")

            address_entries = []
            ada_docs = frappe.get_all("ADA_1", fields=["name"])
            for ada_doc in ada_docs:
                ada_record = frappe.get_doc("ADA_1", ada_doc.name)
                if hasattr(ada_record, "address_table") and ada_record.address_table:
                    for address in ada_record.address_table:
                        address_entries.append({
                            'address_id': address.address_id, 
                            'address_entry': address.address_entry
                        })
            # print("Address Entries.............")
            # print(f"Address Entries: {address_entries}")

            best_match = None
            max_similarity = 0

            # Always perform fuzzy similarity on the full raw delivered_to against all address_entries
            for entry in address_entries:
                similarity_score = similar(delivered_to, entry['address_entry'])
                if similarity_score > max_similarity:
                    max_similarity = similarity_score
                    best_match = entry

            if best_match and max_similarity > 0.6:
                matched_address_id = best_match['address_id']
                # print(f"Best Matching Address ID: {matched_address_id} with similarity {max_similarity}")
            else:
                return {'Status': 'Error', 'Message': "No sufficiently similar address entry found."}

            # Fetch SAP Customer Details
            try:
                print("Fetching SAP Settings...")
                doc_settings = frappe.get_doc('SAP Settings')

                print("Authenticating with SAP B1...")
                session = AuthenticateSAPB1()

                Url = doc_settings.sap_b1_url + f"BusinessPartners?$filter=CardCode eq '{CardCode}'"
                # print(f"Requesting SAP API: {Url}")

                response = session.request("GET", Url, headers=headersList, verify=False)
                # print(f"Response Status: {response.status_code}, Response: {response.text}")

                customer_data = response.json()

                if not customer_data.get('value'):
                    print(f"No customer data found for CardCode: {CardCode}")
                    return {'Status': 'Error', 'Message': f"No customer data found for CardCode: {CardCode}."}

                customer_details = customer_data['value'][0]
                addresses = customer_details.get('BPAddresses', [])
                matched_state = None
                matched_address_name = None
                matched_address_gstin = None

                # Debug: log what we're trying to match in SAP address list
                print(f"Looking up matched_address_id in SAP addresses: {matched_address_id}")
                for address in addresses:
                    if address.get('AddressName') == matched_address_id:
                        matched_address_name = address.get('AddressName')
                        matched_state = address.get('State')
                        matched_address_gstin = normalize_gstin(
                            address.get('GSTIN') or address.get('FederalTaxID')
                        )
                        print(f"Matched Address Found: {matched_address_name}, State: {matched_state}")
                        break

                if not matched_address_name:
                    # Provide additional debug info when no match is found
                    address_names = [a.get('AddressName') for a in addresses if a.get('AddressName')]
                    print(
                        f"No matching SAP address found for '{matched_address_id}'. "
                        f"Available SAP AddressName values: {address_names}"
                    )
                    msg = f"No matching SAP address found for '{matched_address_id}'."
                    return {'Status': 'Error', 'Message': msg}

                # Prefer GSTIN from matched SAP address row; fallback to BP-level GST fields.
                sap_gstin = matched_address_gstin or normalize_gstin(
                    customer_details.get('GSTIN') or customer_details.get('FederalTaxID')
                )
                print(f"Extracted GST No: {extracted_gst_no or 'Not Found'}")
                print(f"SAP GSTIN: {sap_gstin or 'Not Found'}")

                if not extracted_gst_no or not sap_gstin:
                    return {
                        'Status': 'Error',
                        'Message': (
                            f"GST validation failed for CardCode {CardCode}. "
                            f"Extracted GST: {extracted_gst_no or 'Not Found'}, "
                            f"SAP GSTIN: {sap_gstin or 'Not Found'}."
                        )
                    }

                if extracted_gst_no != sap_gstin:
                    return {
                        'Status': 'Error',
                        'Message': (
                            f"GST mismatch for CardCode {CardCode}. "
                            f"Extracted GST: {extracted_gst_no}, SAP GSTIN: {sap_gstin}."
                        )
                    }

                return {
                    'Status': 'Success',
                    'Result': {
                        'CardCode': customer_details.get('CardCode', ''),
                        'CustomerName': customer_details.get('CardName', ''),
                        'Addresses': addresses,
                        'MatchedAddressName': matched_address_name,
                        'MatchedState': matched_state,
                        'SalesPersonCode': customer_details.get('SalesPersonCode', ''),
                        'GSTIN': sap_gstin
                    }
                }

            except Exception as e:
                print(f"Error in SAP request: {str(e)}")
                return {'Status': 'Error', 'Message': f"Failed to fetch SAP data: {str(e)}"}

        except Exception as e:
            return {'Status': 'Error', 'Message': f"An error occurred: {str(e)}"}






    def read_xlsx_content(xlsx_data):
        """Read and parse XLSX content."""
        xlsx_file = io.BytesIO(xlsx_data)
        workbook = openpyxl.load_workbook(xlsx_file)
        sheet = workbook.active
        header = [cell.value for cell in sheet[1]]

        try:
            item_code_index = header.index("Item Code")
            quantity_index = header.index("Quantity")
            margin_index = header.index("Margin %")
            description_index = header.index("Product Description")
            basic_cost_index = header.index("Basic Cost Price")
            igst_index = header.index("IGST %")
            tax_amount_index = header.index("Tax Amount")
            landing_rate_index = header.index("Landing Rate")
            mrp_index = header.index("MRP")
            total_amount_index = header.index("Total Amount")
        except ValueError:
            print("'Item Code' or 'Quantity' column not found")
            return []

        data = [
            {
                "item_code": row[item_code_index], 
                "quantity": row[quantity_index],
                "Discount": row[margin_index],
                "product_description": row[description_index],
                "basic_cost_price": row[basic_cost_index],
                "igst_percentage": row[igst_index],
                "tax_amount": row[tax_amount_index],
                "landing_rate": row[landing_rate_index],
                "mrp": row[mrp_index],
                "total_amount": row[total_amount_index]
            }
            for row in sheet.iter_rows(min_row=2, values_only=True)
            if all(row[i] is not None for i in [
                item_code_index, quantity_index, margin_index,
                description_index, basic_cost_index, igst_index,
                tax_amount_index, landing_rate_index, mrp_index,
                total_amount_index
            ])
        ]
        return data
        
    
    def save_pdf_to_file(pdf_data, po_number):
        """Save the PDF to S3 inside a dynamically set folder."""
        if not po_number or po_number == "Not Found":
            po_number = "Unknown_PO"

        # Fetch AWS credentials, bucket name, and folder name
        if frappe.get_single("AWS"):
            aws_record = frappe.db.get_value(
                "AWS", None, 
                ["aws_access_key_id", "aws_secret_access_key", "bucket_name", "folder_blinkit"], 
                as_dict=True
            )

            if aws_record:
                aws_access_key_id = aws_record.aws_access_key_id.strip()
                aws_secret_access_key = aws_record.aws_secret_access_key.strip()
                bucket_name = aws_record.bucket_name.strip()
                folder_name = (aws_record.folder_blinkit or "default_folder").strip().strip('/')
            else:
                frappe.throw("AWS record not found in the database.")
        else:
            frappe.throw("AWS table does not exist.")

        # Initialize the S3 client
        s3 = boto3.client(
            's3',
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key
        )

        file_name = f"{folder_name}/Blinkit_{po_number}.pdf"  # Ensure proper path format

        try:
            # Upload the PDF to S3 inside the dynamic folder
            s3.put_object(Bucket=bucket_name, Key=file_name, Body=pdf_data)

            # Construct the correct file URL
            file_url = f"https://{bucket_name}.s3.amazonaws.com/{file_name}"

            # print(f"PDF uploaded successfully: {file_url}")
            return file_url

        except NoCredentialsError:
            print("Credentials not available for accessing AWS S3")
            return None
 
    def fetch_item_sap_mapping():
        # print("Fetching Itemmap records...")
        # print("Checking if Itemmap doctype exists...")
        doctypes = frappe.get_all('DocType', filters={'name': 'Itemmap'})
        # print(f"Doctypes found: {doctypes}")
      
        item_sap_map = []
        
        for item in doctypes:
            ada_record = frappe.get_doc("Itemmap", item.name)
            if hasattr(ada_record, "map_item") and ada_record.map_item:
                for address in ada_record.map_item:
                    item_sap_map.append({
                        'item_code': address.item_code, 
                        'sap_code': address.sap_code
                    })
        
        # print("Address Entries:", item_sap_map)
        return item_sap_map

    def move_email_to_processed(mail, email_uid):
        """Move an email to the Blinkit-Processed folder using IMAP UIDs."""
        try:
            # Ensure the "Blinkit-Processed" folder exists
            mail.create('Blinkit-Processed')
        except imaplib.IMAP4.error:
            pass  # Folder already exists, no need to create it

        try:
            # Copy the email using UID
            copy_result = mail.uid('copy', email_uid, 'Blinkit-Processed')
            if copy_result[0] == 'OK':
                # Mark the original email for deletion using UID
                store_result = mail.uid('store', email_uid, '+FLAGS', '\\Deleted')
                if store_result[0] == 'OK':
                    mail.expunge()  # Permanently delete the email
                    print(f"Moved email UID {email_uid} to Blinkit-Processed folder.")
                    return True
                else:
                    print(f"Failed to mark email UID {email_uid} as deleted: {store_result}")
                    return False
            else:
                print(f"Failed to copy email UID {email_uid}: {copy_result}")
                return False
        except Exception as e:
            print(f"Error moving email UID {email_uid}: {str(e)}")
            return False

    @frappe.whitelist()
    def itemmaster(item_list, code_sheet_data):
        """Fetch from item master using a list of item codes, mapping to SAP codes."""
        # print("Received item_list:......................")
        # print("Received code_sheet_data...................")

        doc_settings = frappe.get_doc('SAP Settings')
        session = AuthenticateSAPB1()  # Authenticate with SAP B1
        payload = ''
        results = []
        unmapped_items = []

        # Ensure code_sheet_data is a dictionary
        if isinstance(code_sheet_data, list):
            # Convert list of dictionaries to a single dictionary
            code_sheet_data = {item['item_code']: item['sap_code'] for item in code_sheet_data}

        # Create a mapping from item_code to sap_code
        item_to_sap_map = code_sheet_data

        for item in item_list:
            item_code = str(item['item_code'])  # Ensure it's a string for dictionary lookup
            # print(f"Checking item_code: {item_code} in mapping...")

            # Check if the item_code exists in the mapping
            sap_code = item_to_sap_map.get(item_code)

            if sap_code:
                # print(f"Mapped SAP item code for {item_code}: {sap_code}")

                url = doc_settings.sap_b1_url + f"Items('{sap_code}')"  # Corrected to use sap_code
                try:
                    response = session.request("GET", url, data=payload, headers=headersList, verify=False)

                    if response.status_code == 200:
                        item_data = response.json()
                        item_name = item_data.get('ItemName', 'N/A')
                        tax_rate = item_data.get('U_TaxRate', 'N/A')
                        gst_tax = item_data.get('U_GstTax', 'N/A')
                        igst_tax = item_data.get('U_IgstTax', 'N/A')

                        results.append({
                            "ItemCode": item_code,
                            "SAPCode": sap_code,
                            "ItemName": item_name,
                            "U_TaxRate": tax_rate,
                            "U_GstTax": gst_tax,
                            "U_IgstTax": igst_tax
                        })
                    else:
                        print(f"Error fetching data for SAP code {sap_code} (Item code {item_code}): {response.text}")

                except Exception as e:
                    print(f"Exception occurred while fetching data for SAP code {sap_code} (Item code {item_code}): {e}")
            else:
                unmapped_items.append(item_code)
                print(f"No mapping found for item code {item_code}")
                pass

        if unmapped_items:
            error_message = f"No mapping found for item codes: {', '.join(unmapped_items)}"
            print(error_message)
            return {"Status": "Error", "Message": error_message}

        if results:
            return {"data": results}
        else:
            error_message = "No matching records found for any item codes."
            print(error_message)
            return {"Status": "Error", "Message": error_message}

    @frappe.whitelist()
    def process_b2b_data(json_data):
        """
        Process the JSON data and store it in the B2B doctype.
        """
        try:
            # Ensure json_data is not None
            if not json_data:
                print("Error: JSON data is None or empty.")
                return

            # Ensure json_data is a dictionary (if it's passed as a string, parse it)
            if isinstance(json_data, str):
                json_data = frappe.parse_json(json_data)

            # Safely extract data from JSON with default values
            po_number = json_data.get("po_number", "N/A")  # Default to "N/A" if po_number is missing
            email_id = json_data.get("email_id", "N/A")  # Extract email_id from json_data
            extracted_data = json_data.get("extracted_data", {})
            customer_details = json_data.get("customer_details", {})
            file_url = json_data.get("file_url", "")
            item_data_response = json_data.get("item_data_response", {}).get("data", [])

            # Check if B2B doctype exists
            if not frappe.db.table_exists("B2B"):
                print('Doctype "B2B" does not exist.')
                return

            # Check if the record already exists
            existing_record_name = frappe.db.exists("B2B", {"po_number": po_number})
            updated = False

            if existing_record_name:
                # Update existing record
                doc = frappe.get_doc("B2B", existing_record_name)
                updated = True
            else:
                # Create new record
                doc = frappe.new_doc("B2B")

            # Update fields from extracted_data
            if extracted_data:
                for field, value in extracted_data.items():
                    if hasattr(doc, field):
                        setattr(doc, field, value)

            # Update fields from customer_details
            if customer_details:
                doc.customer_code = customer_details.get("CardCode", "")
                doc.employee_id = customer_details.get("SalesPersonCode", "")

                # Handle MatchedAddress (it can be None)
                matched_address = customer_details.get("MatchedAddress")
                if matched_address is not None:
                    doc.matched_address_name = matched_address.get("AddressName", "")
                else:
                    doc.matched_address_name = None  # Explicitly set to None if MatchedAddress is null

                # Handle MatchedState (it can be None)
                matched_state = customer_details.get("MatchedState")
                doc.matched_state = matched_state

            # Update file URL
            doc.po_url = file_url

            # Set default values for Billing_from and Billto
            doc.billing_from = "KT"  # Default value for Billing_from
            doc.billto = "Local" if matched_state == "KT" else "Central"

            # Set email_id field
            doc.email_id = email_id  # Store the email_id in the B2B doctype

            # Initialize totals
            total_quantity = 0
            total_amount = 0
            unique_items = set()

            # Clear existing child table rows (if updating)
            if updated:
                doc.b2b_table = []

            # Add item details to the child table and accumulate totals
            if item_data_response:
                for item in item_data_response:
                    quantity = item.get("Quantity", 0)
                    total_item_amount = item.get("TotalAmount", 0)
                    item_code = item.get("ItemCode", "")
                    
                    # Determine the taxcode format based on matched_state
                    tax_prefix = "KACS" if matched_state == "KT" else "KAIG"
                    
                    # Get the tax_rate from the current item
                    tax_rate = int(item.get("U_TaxRate", 0))  # Ensure tax_rate is an integer
                    
                    # Format the taxcode
                    taxcode = f"{tax_prefix}{tax_rate}"

                    # Append item to child table
                    doc.append("b2b_table", {
                        "item_code": item_code,
                        "sap_code": item.get("SAPCode", ""),
                        "item_name": item.get("ItemName", ""),
                        "quantity": quantity,
                        "discount": item.get("Discount", 0),
                        "product_description": item.get("ProductDescription", ""),
                        "basic_cost_price": item.get("BasicCostPrice", 0),
                        "igst_percentage": item.get("IGSTPercentage", 0),
                        "tax_amount": item.get("TaxAmount", 0),
                        "landing_rate": item.get("LandingRate", 0),
                        "mrp": item.get("MRP", 0),
                        "total_amount": total_item_amount,
                        "tax_rate": item.get("U_TaxRate", 0),
                        "gst_tax": item.get("U_GstTax", ""),
                        "igst_tax": item.get("U_IgstTax", ""),
                        "taxcode": taxcode
                    })

                    # Accumulate totals
                    total_quantity += quantity
                    total_amount += total_item_amount
                    unique_items.add(item_code)

            # Update total fields in parent doctype
            doc.total_quantity = total_quantity
            doc.total_amount = total_amount
            doc.total_items = len(unique_items)

            # Save or insert the document
            if updated:
                doc.save()
                print(f"Record updated for P.O. Number: {po_number}.")
            else:
                # Generate a unique name for the new record
                doc.name = f"{po_number}-{frappe.generate_hash(length=8)}"
                doc.insert()
                print(f"New record created for P.O. Number: {po_number}.")

        except Exception as e:
            # Log the error and print a message
            print(f"Error processing B2B data: {str(e)}")
            frappe.log_error(f"Error in process_b2b_data: {str(e)}")

    def read_attachments(mail, CardCode):
        """Read email attachments using UIDs"""
        try:
            mail.select(f'"{LABEL_NAME}"')  # Select the label
            status, messages = mail.uid('search', None, 'ALL')  # Search for unread emails
            email_uids = messages[0].split()
            print(f"Found {len(email_uids)} emails in label {LABEL_NAME}.")

            for email_uid in email_uids:
                email_uid_str = email_uid.decode()
                print(f"Processing email UID: {email_uid_str}")

                try:
                    status, msg_data = mail.uid('fetch', email_uid_str, '(RFC822)')
                    for response_part in msg_data:
                        if isinstance(response_part, tuple) and isinstance(response_part[1], bytes):
                            msg = email.message_from_bytes(response_part[1])
                        elif isinstance(response_part, bytes):
                            try:
                                msg = email.message_from_bytes(response_part)
                            except Exception as e:
                                print(f"Error processing response_part: {e}")
                                send_error_email(
                                    po_number="Unknown",
                                    error_message=f"Error processing email response_part for UID {email_uid_str}: {str(e)}",
                                    file_url=None,
                                    channel_name="Blinkit",
                                )
                                continue
                        else:
                            print(f"Skipping unexpected response_part: {type(response_part)}")
                            continue
                        
                        if msg.is_multipart():
                            pdf_data = xlsx_data = None
                            found_valid_pdf = False
                            pdf_text = ""   #by harsha
                            extracted_data2 = {}  # Initialize extracted_data2
                            po_number = "Unknown"
                            file_url = None
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                filename = part.get_filename()

                                if filename:
                                    if filename.lower().endswith(".pdf"):
                                        pdf_data = part.get_payload(decode=True)
                                        if isinstance(pdf_data, bytes):
                                            # print(f"Found PDF attachment: {filename}")
                                            # pdf_data = rebuild_pdf_with_pikepdf(pdf_data)  # Rebuild PDF with pikepdf
                                            pdf_text = extract_pdf_text(pdf_data)
                                            if is_valid_po_pdf_text(pdf_text):
                                                found_valid_pdf = True
                                                print("Found valid PDF:", filename)  # Keep for debug
                                            else:
                                                # print("Invalid PDF (pypdf extraction): Missing required markers.")
                                                # Attempt Plumber extraction if initial extraction fails
                                                pdf_text = extract_text_from_pdf_with_plumber(pdf_data)

                                                # print("Plumber extracted text length:", len(pdf_text or ""))
                                                if is_valid_po_pdf_text(pdf_text):
                                                    found_valid_pdf = True
                                                    print("Found valid PDF with plumber:", filename)  # Keep for debug
                                                    extracted_data2 = extract_data_from_plumber(pdf_text)  # Call the new function here
                                                    # print("Extracted Data from plumber:", extracted_data2)

                                                else:
                                                    print("Invalid PDF even after plumber extraction.")
                                                    send_error_email(
                                                        po_number="Unknown",
                                                        error_message=f"Invalid Blinkit PDF after extraction attempts. UID {email_uid_str}, file {filename}",
                                                        file_url=None,
                                                        channel_name="Blinkit",
                                                    )
                                        else:
                                            # print(f"Error: PDF data is not bytes-like for {filename}")
                                            pdf_data = None  # Reset pdf_data to None if it's not bytes-like

                                    elif filename.lower().endswith(".xlsx"):
                                        print(f"Found XLSX attachment: {filename}")
                                        xlsx_data = part.get_payload(decode=True)
                                        if not isinstance(xlsx_data, bytes):
                                            # print(f"Error: XLSX data is not bytes-like for {filename}")
                                            xlsx_data = None  # Reset xlsx_data to None if it's not bytes-like

                            if pdf_text and found_valid_pdf :
                                # print("Processing PDF and XLSX data...")  # Keep for debug
                                extracted_data = extract_data(pdf_text)
                                extracted_data = merge_extracted_data(extracted_data, extracted_data2)  # Merge data

                                # If pypdf extraction missed key fields, do a second pass with pdfplumber
                                # and fill only missing values. This fixes cases like PO 2173110010781 where
                                # "Delivered To" / company name may be dropped by pypdf but present via plumber.
                                if pdf_data and (
                                    extracted_data.get("company_name") in (None, "Not Found")
                                    or extracted_data.get("delivered_to") in (None, "Not Found")
                                ):
                                    # print("Key fields missing from pypdf text; retrying extraction with pdfplumber...")
                                    plumber_text = extract_text_from_pdf_with_plumber(pdf_data)
                                    # print("Plumber retry text length:", len(plumber_text or ""))
                                    if plumber_text:
                                        plumber_data = extract_data(plumber_text)
                                        extracted_data = merge_extracted_data(extracted_data, plumber_data)
                                        # print("Merged data after plumber retry:", extracted_data)

                                # Targeted debug (only when still missing)
                                if extracted_data.get("company_name") in (None, "Not Found") or extracted_data.get("delivered_to") in (None, "Not Found"):
                                    try:
                                        dbg_source = plumber_text if 'plumber_text' in locals() and plumber_text else pdf_text
                                        po_idx = dbg_source.find("Purchase Order")
                                        del_idx = dbg_source.find("Delivered")
                                        if po_idx != -1:
                                            snippet = dbg_source[max(0, po_idx - 100): po_idx + 400]
                                            # print("DEBUG snippet near 'Purchase Order':", snippet)
                                        if del_idx != -1:
                                            snippet = dbg_source[max(0, del_idx - 100): del_idx + 700]
                                            # print("DEBUG snippet near 'Delivered':", snippet)
                                    except Exception as e:
                                        # print("DEBUG snippet failed:", e)
                                        pass
                                
                                po_number = extracted_data.get("po_number")
                                item_list = read_xlsx_content(xlsx_data) if xlsx_data else []
                                print("PO Number:", po_number)  # Keep for debug
                                print("GST No:", extracted_data.get("gst_no", "Not Found"))  # Keep for debug

                                customer_details = Get_CustomerDetails(CardCode, extracted_data,extracted_data2)
                                if not isinstance(customer_details, dict) or customer_details.get("Status") != "Success":
                                    print("Customer details lookup failed:", customer_details)
                                    send_error_email(
                                        po_number=po_number,
                                        error_message=f"Customer details lookup failed for UID {email_uid_str}: {customer_details}",
                                        file_url=None,
                                        channel_name="Blinkit",
                                    )
                                    continue

                                if po_number:
                                    try:
                                        file_url = save_pdf_to_file(pdf_data, po_number)
                                        print(f"File saved at: {file_url}")  # Keep for debug
                                        
                                        code_sheet_data = fetch_item_sap_mapping()
                                        item_data_response = itemmaster(item_list, code_sheet_data)

                                        if item_data_response.get("Status") == "Error":
                                            send_error_email(
                                                po_number=po_number,
                                                error_message=item_data_response["Message"],
                                                file_url=file_url,
                                                channel_name="Blinkit",
                                            )
                                            continue

                                        merged_item_data = []
                                        item_response_dict = {item["ItemCode"]: item for item in item_data_response.get("data", [])}

                                        for item in item_list:
                                            item_code = item["item_code"]
                                            if str(item_code) in item_response_dict:
                                                merged_item = {
                                                    **item_response_dict[str(item_code)],
                                                    "Quantity": item["quantity"],
                                                    "Discount": item["Discount"],
                                                    "ProductDescription": item["product_description"],
                                                    "BasicCostPrice": item["basic_cost_price"],
                                                    "IGSTPercentage": item["igst_percentage"],
                                                    "TaxAmount": item["tax_amount"],
                                                    "LandingRate": item["landing_rate"],
                                                    "MRP": item["mrp"],
                                                    "TotalAmount": item["total_amount"]
                                                }
                                                merged_item_data.append(merged_item)
                                            else:
                                                merged_item_data.append({
                                                    "ItemCode": item["item_code"],
                                                    "Quantity": item["quantity"],
                                                    "Discount": item["Discount"],
                                                    "ProductDescription": item["product_description"],
                                                    "BasicCostPrice": item["basic_cost_price"],
                                                    "IGSTPercentage": item["igst_percentage"],
                                                    "TaxAmount": item["tax_amount"],
                                                    "LandingRate": item["landing_rate"],
                                                    "MRP": item["mrp"],
                                                    "TotalAmount": item["total_amount"]
                                                })
                                        
                                        item_data_response["data"] = merged_item_data

                                        matched_address = None
                                        if customer_details['Status'] == 'Success':
                                            matched_address = next((address for address in customer_details['Result']['Addresses'] if address['AddressName'] == customer_details['Result']['MatchedAddressName']), None)
                                            if matched_address:
                                                matched_address = {
                                                    "AddressName": matched_address["AddressName"],
                                                    "State": matched_address["State"]
                                                }

                                        json_data = {
                                            "po_number": po_number,
                                            "email_id": email_uid_str,
                                            "extracted_data": extracted_data,
                                            "customer_details": {
                                                "CardCode": customer_details['Result']['CardCode'],
                                                "SalesPersonCode": SalesPersonCode,
                                                "CustomerName": customer_details['Result']['CustomerName'],
                                                "MatchedAddress": matched_address,
                                                "MatchedState": customer_details['Result']['MatchedState']
                                            },
                                            "file_url": file_url,
                                            "item_data_response": item_data_response
                                        }

                                        process_b2b_data(json_data)

                                        if move_email_to_processed(mail, email_uid_str):
                                            print(f"Moved email UID {email_uid_str}")  # Keep for debug
                                            pass
                                        else:
                                            print(f"Failed to move email {email_uid_str}.")
                                            send_error_email(
                                                po_number=po_number,
                                                error_message=f"Failed to move email UID {email_uid_str} to Blinkit-Processed.",
                                                file_url=file_url,
                                                channel_name="Blinkit",
                                            )

                                    except Exception as e:
                                        error_message = f"Unexpected error processing email UID {email_uid_str}: {str(e)}"
                                        print(error_message)
                                        send_error_email(
                                            po_number=po_number,
                                            error_message=error_message,
                                            file_url=file_url if 'file_url' in locals() else None,
                                            channel_name="Blinkit",
                                        )
                                        continue

                                    # with open(f"{po_number}.json", "w") as json_file:
                                    #     json.dump(json_data, json_file, indent=4)

                except Exception as e:
                    print(f"Error processing email UID {email_uid_str}: {e}")  # Keep for debug
                    send_error_email(
                        po_number="Unknown",
                        error_message=f"Unhandled error processing email UID {email_uid_str}: {str(e)}\n\n{traceback.format_exc()}",
                        file_url=None,
                        channel_name="Blinkit",
                    )
                    continue  # Continue with the next email

        except Exception as e:
            print(f"Error occurred: {e}")  # Keep for debug
            send_error_email(
                po_number="Unknown",
                error_message=f"Error occurred while reading Blinkit attachments: {str(e)}\n\n{traceback.format_exc()}",
                file_url=None,
                channel_name="Blinkit",
            )
            return {"Status": "Failed", "Message": str(e)}



    print("Starting email attachment processing...")  # Keep for debug
    try:
        mail = connect_to_email()
        # CardCode = "C03570"
        result = read_attachments(mail, CardCode)
        # print(result)  # Keep for debug
        mail.logout()
        # print("Finished email attachment processing.")  # Keep for debug
    except Exception as e:
        frappe.log_error(f"Error in process_email_attachments: {str(e)}")
        print(f"An error occurred: {str(e)}")  # Keep for debug
        send_error_email(
            po_number="Unknown",
            error_message=f"Error in Blinkit process_email_attachments: {str(e)}\n\n{traceback.format_exc()}",
            file_url=None,
            channel_name="Blinkit",
        )

# Mail notifications (same pattern as other channels, e.g. Bigbasket.py)
def send_error_email(po_number, error_message, file_url=None, channel_name="Blinkit"):
    """Send an error email using the HTML template."""
    try:
        print(f"send_error_email called for PO {po_number}, channel {channel_name}")

        # Fetch all users with the role 'B2B-Blinkit'
        recipients = frappe.get_all(
            "Has Role",
            filters={"role": "B2B-Blinkit"},
            fields=["parent"],
        )
        if not recipients:
            print("No recipients found with the role 'B2B-Blinkit'.")
            frappe.log_error("No recipients found with the role 'B2B-Blinkit'.")
            return

        recipient_data = []
        for user in recipients:
            email_id = frappe.db.get_value("User", user["parent"], "email")
            enabled = frappe.db.get_value("User", user["parent"], "enabled")
            if email_id and enabled:
                name = frappe.db.get_value("User", user["parent"], "full_name") or "Team"
                recipient_data.append({"email": email_id, "name": name})

        if not recipient_data:
            print("No enabled users with valid email addresses found.")
            frappe.log_error("No enabled users with valid email addresses found.")
            return

        template_path = os.path.join(os.path.dirname(__file__), "email_template.html")
        if not os.path.exists(template_path):
            frappe.log_error(f"Email template not found at {template_path}.")
            return

        with open(template_path, "r") as file:
            html_template = file.read()

        # Send individual emails
        for recipient in recipient_data:
            po_link_html = (
                f'<a href="{str(file_url).strip()}" target="_blank" rel="noopener noreferrer">Open PDF</a>'
                if file_url and str(file_url).strip()
                else "N/A"
            )
            html_content = (
                html_template
                .replace("{{ recipient_name }}", recipient["name"])
                .replace("{{ po_number or \"N/A\" }}", str(po_number or "N/A"))
                .replace("{{ error_message or \"No details provided.\" }}", str(error_message or "No details provided."))
                .replace("{{ file_url or \"\" }}", str(file_url or ""))
                .replace("{{ po_link_html }}", po_link_html)
                .replace("{{ channel_name }}", str(channel_name))
            )

            print(f"Sending email to: {recipient['email']} ({recipient['name']})")
            try:
                frappe.sendmail(
                    recipients=[recipient["email"]],
                    subject=f"Notification from B2B {channel_name} Channel - PO Number: {po_number}",
                    message=html_content,
                )
                print("Email sent successfully.")  # Keep for debug
            except Exception as e:
                print(f"Error while sending email to {recipient['email']}: {str(e)}")  # Keep for debug
                frappe.log_error(f"Error while sending email to {recipient['email']}: {str(e)}")

    except Exception as e:
        frappe.log_error(f"Failed to send error email: {str(e)}")
        print(f"Failed to send error email: {str(e)}")  # Keep for debug

# Call the main function 
if __name__ == "__main__":
    process_email_attachments()

# bench --site dev.khanaltech.com execute  khanal_tech_integrations.utils.B2B.Blink.process_email_attachments
# bench --site khanaltest.localhost execute  khanal_tech_integrations.utils.B2B.Blink.process_email_attachments