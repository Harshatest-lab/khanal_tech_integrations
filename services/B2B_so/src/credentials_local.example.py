"""Copy to credentials_local.py (gitignored) for local runs."""

DATABASE_URL = ""

IMAP = {
    "email": "",
    "password": "",
    "imap_server": "imap.gmail.com",
    "imap_port": 993,
    # Legacy single-channel (Blinkit only):
    "folder": "Blinkit",
    # Preferred — one shared mailbox, one Gmail label per channel:
    "folders": {
        "blinkit": "Blinkit",
        "zepto": "Zepto",
        "instamart": "Instamart",
        "bigbasket": "Bigbasket",
    },
    # SAP Business Partner CardCode per channel (blinkit defaults to C03570 in code):
    "card_codes": {
        "blinkit": "C03570",
        # "zepto": "Cxxxxx",
        # "instamart": "Cxxxxx",
        # "bigbasket": "Cxxxxx",
    },
}

# Salesperson codes live in b2b.users, not here.
