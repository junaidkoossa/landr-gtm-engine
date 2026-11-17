import csv

with open("data/input_creators.csv", encoding="utf-8-sig") as ingestion: 
    reader = csv.DictReader(ingestion)
    for row in reader:
        print(row)
        if not row["name"].strip():
            print("Missing name")
        if not row["website_url"].strip():
            print("Missing website")
        if not row["target_type"].strip():
            print("Missing target type")

