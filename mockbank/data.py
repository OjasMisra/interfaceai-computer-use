"""Synthetic member data for the mock core-banking app.

Everything here is fake. SSNs use the 900-series, which the SSA never issues.
"""

MEMBERS = {
    "10042": {
        "name": "DOE, JANE Q",
        "ssn": "900-12-3456",
        "dob": "04/12/1980",
        "address": "12 TEST LANE, SPRINGFIELD",
        "phone": "(555) 010-4242",
        "shares": [
            {"id": "S00", "type": "SAVINGS", "desc": "PRIMARY SHARE SAVINGS", "balance": "1,250.75", "available": "1,245.75"},
            {"id": "S10", "type": "CHECKING", "desc": "SHARE DRAFT", "balance": "342.10", "available": "342.10"},
        ],
    },
    # Shares deliberately in a different order: an index-based cell locator
    # recorded on 10042 would read the wrong row here.
    "10077": {
        "name": "ROE, RICHARD",
        "ssn": "900-55-0077",
        "dob": "11/02/1971",
        "address": "77 SAMPLE ST, SHELBYVILLE",
        "phone": "(555) 010-7777",
        "shares": [
            {"id": "S10", "type": "CHECKING", "desc": "SHARE DRAFT", "balance": "88.00", "available": "88.00"},
            {"id": "S05", "type": "CLUB", "desc": "HOLIDAY CLUB", "balance": "410.00", "available": "410.00"},
            {"id": "S00", "type": "SAVINGS", "desc": "PRIMARY SHARE SAVINGS", "balance": "18,004.22", "available": "17,999.22"},
        ],
    },
    # Employee account: tellers without supervisor rights are denied.
    "10013": {
        "name": "EMPLOYEE, RESTRICTED",
        "ssn": "900-00-0013",
        "dob": "01/01/1990",
        "address": "1 HQ PLAZA",
        "phone": "(555) 010-0013",
        "restricted": True,
        "shares": [],
    },
}

OPERATORS = {"TELLER01": "demo-pass-123"}
