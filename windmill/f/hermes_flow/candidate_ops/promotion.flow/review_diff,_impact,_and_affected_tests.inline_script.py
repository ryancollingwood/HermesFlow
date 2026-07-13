import wmill

def main(evidence):
    urls = wmill.get_resume_urls()
    return {
        "resume": urls["resume"],
        "cancel": urls["cancel"],
        "description": {
            "render_all": [
                "# Candidate promotion approval",
                evidence["evidence"]["promotion_summary"]["text"],
                {"json": evidence},
            ]
        },
    }