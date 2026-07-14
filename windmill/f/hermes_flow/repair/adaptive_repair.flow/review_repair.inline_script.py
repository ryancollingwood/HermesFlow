import wmill


def main(evidence):
    urls = wmill.get_resume_urls()
    return {
        "resume": urls["resume"],
        "cancel": urls["cancel"],
        "description": {
            "render_all": [
                "# Adaptive repair approval",
                evidence["promotion"]["evidence"]["promotion_summary"]["text"],
                {"json": {
                    "diff": evidence["promotion"]["evidence"]["diff"],
                    "impact": evidence["promotion"]["evidence"]["impact"],
                    "regression": evidence["regression"],
                    "promoted_fixtures": evidence["promoted_fixtures"],
                }},
            ]
        },
    }
