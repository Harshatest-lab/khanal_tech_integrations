"""Copy to credentials_local.py (gitignored) for local runs."""

DATABASE_URL = ""

IMAP = {
    "email": "",
    "password": "",
    "imap_server": "imap.gmail.com",
    "imap_port": 993,
    "folder": "Blinkit",  # Gmail label for this channel; same mailbox later: Zepto, etc.
}

# Blinkit CardCode is hardcoded in blinkit.py (BLINKIT_CARD_CODE).
# Salesperson codes live in b2b.users, not here.
