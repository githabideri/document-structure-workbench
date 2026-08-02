# Table Detection

## What is table detection?

Table detection is the process of finding regions in a document page that contain tables. This is done using layout analysis models that examine the visual structure of the page.

## Why is this step needed?

Before the system can read a table's content, it must first know where the table is on the page. Table detection provides the bounding box coordinates that define the table region.

## Possible problems

- A table might be missed entirely if it looks like regular text
- Two adjacent tables might be detected as one
- Surrounding text or captions might be included in the table region
- Tables with unusual formatting (no borders, merged cells) might be harder to detect
