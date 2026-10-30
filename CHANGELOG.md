# Changelog

## 2026-10-30 — Publication privacy check

- Removed identifying computer paths, private-network addresses, scanner device identifiers and personal SSH defaults from shared records, source code and the history ZIP. Connection examples now require configuration for the user's own scanner.
- Removed GPS/device metadata from the four setup photographs without changing visible pixels or orientation. Retained a private original backup separately from the release files.
- Updated the distributed-data checksums and documented privacy redactions. All 396 scan images and 40 model results retain their original content; feature values and bounding boxes are unchanged.
- Added a release check for credentials, personal paths, private-network identifiers and photograph GPS coordinates. The 217 workflow tests and all five restored-scan audits passed after cleanup.

## 2026-10-30 — Consolidated paper repository

- Linked the official OpenScan Mini page in the paper methods, bibliography, front page and setup notes. Preserved the author's latest introduction, added the authorised photograph credit and kept the manuscript below 6,000 characters.
- Preserved all 10,452 files from Capture commit `4bd63c7003c547aba9bee743529b8e6b76d55c44`: operating code in `scanner/`, five complete scans and their viewer in `examples/scans/`, and older graph/hackathon material in one history ZIP. A file-by-file manifest and offline checker verify the original bytes.
- Retained 200 original scanner photographs, 196 crops and all 40 model batches, including five failures. Corrected an ignore rule so original response records are included in a normal checkout.
- Added four original setup photographs and ten annotated talk examples, with slide sources and checksums. Applied the photographer's CC BY 4.0 grant to Jack Dan Hollister's photographs, preserved third-party licences, and recorded credits for the historical NHM butterfly images and map data.
- Updated the abstract, caption and character count for Qwen3.8-27B and complete references. The front page gives one installation path, the scanner examples and a closing mention of the later TraitTrail Studio app.
- Fresh installation and the offline demo passed. All 217 tests passed (15 experiment, 98 scanner, 104 saved-scan); all five restored scans passed their integrity audits, and the saved viewer served image evidence with live capture disabled. All 25 rebuilt figure images matched the approved pixels.
- Verified complete source preservation from a clean publication-file copy, citation schema, local links, Git byte preservation, file sizes and credential-pattern checks. Code remains MIT; citation metadata is ready for GitHub/Zenodo.
- Reconciled the shorter front page with the owner's GitHub edits, retaining the personal introduction, presentation location and recorded model identifier. Capture and Studio remain unchanged; this update prepares the consolidated repository for review without changing visibility or creating a Zenodo deposit.

## 2026-10-28 — Model names clarified

- Spelled out vision-language model (VLM) and added the recorded Qwen identifier alongside the exact other-model names on the front page.
- Used the Qwen identifier recorded with the saved examples, checked model names against the run catalogue and refreshed the preview.

## 2026-10-27 — Simpler project front page

- Shortened the front page to the experiment, Qwen figure, offline demo and a compact other-model gallery.
- Moved detailed commands, research notes, credits and archive references into one notes page under docs; the front page no longer links readers to other repositories.
- Removed the unrelated older graph discussion from the current project notes and grouped adapted-code licensing under docs.
- Kept runnable code, saved results, original photographs and all approved paper/supplementary figures. All fifteen tests, source checks, local links and package build passed; gallery and setup navigation checked in the browser. No repository rename, commit or push.

## 2026-10-27 — Runnable experiment and shorter documentation

- Added an installable local vision-model workflow, offline image/label review, CSV export and an evidence graph that counts specimens rather than repeated views.
- Catalogued twelve historical runs and restored the eight omitted input views from the two five-view Codex runs; original source bytes and approved figures are retained.
- Consolidated sixteen Markdown files into four: project README, methods/sources, changelog and paper draft. Removed empty prompt/metadata templates; the current runnable prompt is separate from missing historical Qwen prompts.
- Added portable font selection, separate figure output directories, offline/API tests and the approved MIT software licence.
- Kept unresolved historical model, prompt, hardware and image-permission details explicit.

## 2026-10-27 — Supplementary material for GitHub

- Kept the approved full twelve-panel figure for the paper: three specimens, each with an unlabelled photograph and three feature views.
- Added a supplementary-material index linking the print-resolution figure, caption, individual panels, original photographs, saved results and all the other-model examples.
- Checked the selected source hashes and unchanged observation boxes, image files, local links, abstract character count and owner commit details for the private repository upload.

## 2026-10-27 — Full model gallery on the front page

- Added all six prepared other-model figures directly to the front page: Codex's beetle condition check and shell pattern, Claude's wing regions, Gemini's damage prediction, Moonshot Kimi's leg regions and GPT-6 Astra's API body-hair example.
- Retained the local Qwen figure as the lead example and kept each later result labelled with its recorded model and prediction status.
- Verified figure links and refreshed the local preview. No figure data, paper text or remote repository changes.

## 2026-10-27 — Specimen figures for review

- Replaced the three image slots with selected saved Qwen examples: a scanner beetle, a spread-wing butterfly and the yellow closed-wing Naturalis specimen.
- Prepared a four-column, three-row paper figure at 600 dpi, twelve separate GitHub panels and three specimen strips, with consistent crops within each row.
- Added six separately labelled examples from Codex, Claude, Gemini, Moonshot's Kimi and GPT-6 Astra through the API. The Codex beetle condition check retains its unknown damage answer.
- Retained original photographs/results and source metadata, recorded display crops/rotation and selected boxes, and added photo credits and a draft caption.
- Original box coordinates and model answers are unchanged; later 27B-labelled Qwen examples are distinguished from the original 32B talk work.
- Figure/source validation and visual checks completed locally. Exact Qwen prompts, raw replies, model-build/hardware details and final reuse terms remain to complete. No new model run, commit or push.

## 2026-10-27 — Clean paper companion

- Started a separate repository for the TDWG extended abstract, retaining the earlier Capture repository as an archive.
- Added the revised local Qwen/Dell GB10 introduction, graph explanation and later TraitTrail/model connections.
- Added three clearly marked image slots, a prompt placeholder and per-example metadata template for new Qwen results.
- Included the paper draft, hardware/model references, citation details, image-credit guidance and remaining publication steps.
- Validated local links, SVG and JSON syntax, author metadata and the draft abstract's character count.
