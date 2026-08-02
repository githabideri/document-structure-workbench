# Document Processing

## Overview

The Document Structure Workbench processes documents through several stages to extract and validate structured content like tables.

## Processing Stages

1. **Document received** — The PDF or image is uploaded and validated
2. **Pages prepared** — Each page is rendered as an image for analysis
3. **Page contents identified** — Layout detection finds text, tables, figures, and other regions
4. **Tables located** — Table regions are cropped and prepared for structure extraction
5. **Structured results extracted** — Table structure (rows, columns, headers) is recognized
6. **Results checked** — Extractions are compared against ground truth or reviewed by humans

## What Can Go Wrong?

- **Detection errors**: A table might be missed or merged with neighboring content
- **Crop errors**: The cropped region might include too much or too little context
- **Structure errors**: Rows, columns, or headers might be misidentified
- **Text errors**: Cell content might be missing or incorrect
