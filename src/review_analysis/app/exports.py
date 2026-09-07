import csv
import io
import json

from review_analysis.reviews.models import Collection


def reviews_json(collection: Collection) -> str:
    return json.dumps(
        [
            review.model_dump(mode="json", exclude={"cleaned_text"})
            for review in collection.reviews()
        ],
        ensure_ascii=False,
        indent=2,
    )


def spreadsheet_safe(value: str) -> str:
    # Quoting alone does not prevent spreadsheet formula execution.
    return (
        "'" + value
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
        else value
    )


def reviews_csv(collection: Collection) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["id", "title", "text", "rating", "updated_at", "app_version"])
    for review in collection.reviews():
        writer.writerow(
            [
                spreadsheet_safe(review.id),
                spreadsheet_safe(review.title),
                spreadsheet_safe(review.text),
                review.rating,
                review.updated_at.isoformat() if review.updated_at else "",
                spreadsheet_safe(review.app_version or ""),
            ]
        )
    return output.getvalue()
