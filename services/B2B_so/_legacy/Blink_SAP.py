# -*- coding: utf-8 -*-
import frappe
from khanal_tech_integrations.utils.sap import AuthenticateSAPB1
import json
from frappe import _
headersList = {
    "Accept": "*/*",
    "User-Agent": "Khanal Tech",
    "Content-Type": "application/json",
    "Prefer": "odata.maxpagesize=100"
}

@frappe.whitelist()
def send_b2b_data_to_sap(docname):
    """Handle button click - directly send data to SAP on Approve or Update"""
    try:
        doc = frappe.get_doc("B2B", docname)
        
        if not frappe.has_permission("B2B", "write", doc=doc):
            frappe.throw(_("You don't have permission to perform this action"), frappe.PermissionError)

        result = process_sap_integration(doc)
        
        if result.get("status") == "success":
            frappe.db.set_value("B2B", doc.name, {
                "sap_status": "SENT TO SAP",
                "docnum": result.get("docnum", ""),
                "docentry": result.get("docentry", "")
            })
            
            frappe.msgprint(
                msg=result.get("message"),
                title="SAP Integration Success",
                indicator="green"
            )
        else:
            frappe.msgprint(
                msg=result.get("message"),
                title="SAP Integration Error",
                indicator="red"
            )
        
        return result
    except Exception as e:
        frappe.log_error(title="SAP Integration Failed", message=str(e))
        frappe.msgprint(
            msg=f"Failed to process SAP integration: {str(e)}",
            title="Error",
            indicator="red"
        )
        return {"status": "error", "message": str(e)}

def process_sap_integration(doc):
    try:
        doc_settings = frappe.get_doc('SAP Settings')
        session = AuthenticateSAPB1()
        
        base_url = doc_settings.sap_b1_url.rstrip('/')
        if not base_url.endswith('/b1s/v1'):
            base_url = f"{base_url}/b1s/v1"

        frappe.log_error("SAP Integration Debug", f"Base URL: {base_url}")

        sap_payload = {
            "CardCode": doc.customer_code,
            "CardName": doc.company_name,
            "Series": 514,
            "NumAtCard": doc.po_number,
            "DocDueDate": str(doc.po_expiry_date),
            "U_BillingFrom": doc.billing_from,
            "U_BillTo": doc.billto,
            "SalesPersonCode": doc.employee_id,
            "ShipToCode": doc.matched_address_name,
            "PayToCode": doc.matched_address_name,
            "U_PO_LINK": doc.po_url,
            "DocumentLines": []
        }

        for item in doc.b2b_table:
            line_item = {
                "ItemCode": item.sap_code,
                "Quantity": item.quantity,
                "Price": item.basic_cost_price,
                "TaxCode": item.taxcode,
                "UnitPrice": item.basic_cost_price,
               
            }
            sap_payload["DocumentLines"].append(line_item)

        if doc.docentry:
            return handle_sap_update(doc, session, base_url, sap_payload, headersList)
        else:
            return handle_sap_create(doc, session, base_url, sap_payload, headersList)

    except Exception as e:
        error_msg = f"SAP Integration Error: {str(e)}"
        frappe.log_error(title="SAP Integration Failed", message=error_msg)
        handle_error(doc, error_msg)
        return {"status": "error", "message": error_msg}

def handle_sap_create(doc, session, base_url, payload, headers):
    """Handle new SAP order creation"""
    try:
        url = f"{base_url}/Orders"
        frappe.log_error("SAP Create Order", f"URL: {url}, Payload: {payload}")
        
        response = session.post(
            url,
            json=payload,
            headers=headers,
            verify=False,
            
        )
        
        response.raise_for_status()
        response_data = response.json()

        # Ensure data is stored in Frappe
        frappe.db.set_value("B2B", doc.name, {
            "docentry": response_data.get('DocEntry'),
            "docnum": response_data.get('DocNum'),
            "document_status": response_data.get('DocumentStatus'),
            "sap_status": "SENT TO SAP",
            "update_status": "Approved"  
           
        })
        frappe.db.commit()  # Ensure commit

        doc.reload()  # Reload the document to reflect changes
        
        return {
            "status": "success",
            "message": f"Created SAP Order {response_data.get('DocEntry')}",
            "docnum": response_data.get('DocNum'),
            "docentry": response_data.get('DocEntry')
        }
    
    except Exception as e:
        error_msg = f"SAP Create Order Error: {str(e)}"
        frappe.log_error("SAP General Error", error_msg)
        handle_error(doc, error_msg)
        return {"status": "error", "message": error_msg}

def handle_sap_update(doc, session, base_url, payload, headers):
    """Update an existing SAP Order"""
    try:
        if not doc.docentry:
            return {"status": "error", "message": "DocEntry is required for update"}

        url = f"{base_url}/Orders({doc.docentry})"
        # Send only the fields that need updating
        update_payload = {
            "DocumentLines": payload["DocumentLines"],
            "DocDueDate": payload["DocDueDate"]
        }

        response = session.patch(
            url,
            json=update_payload,
            headers={**headers, "Prefer": "return-no-content"},  # Avoid unnecessary response
            verify=False,
            
        )
        response.raise_for_status()

        frappe.db.set_value("B2B", doc.name, {
            "sap_status": "SENT TO SAP",
            "docnum": doc.docnum,
            "docentry": doc.docentry,
            "update_status": "Updated" 
        
        })
        frappe.db.commit()

        return {
            "status": "success",
            "message": f"Updated SAP Order {doc.docentry}",
            "docnum": doc.docnum,
            "docentry": doc.docentry,

        }
    except Exception as e:
        frappe.log_error("SAP Update Order Error", str(e))
        return {"status": "error", "message": str(e)}


def handle_error(doc, error_msg):
    """Centralized error handling"""
    frappe.db.set_value("B2B", doc.name, {"error_status": error_msg})
    frappe.log_error(title="SAP Integration Error", message=error_msg)



    # bench --site alpha.localhost execute khanal_tech_integrations.utils.B2B.Blink_SAP.send_b2b_data_to_sap   "1256710130228"
    