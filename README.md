# LGE 2026 Ward Flip Tracker (unofficial)

A personal tracker for the South African 2026 local government elections. It reads the
public IEC results feed and shows which wards have changed hands compared with the
November 2021 election, which wards are *leading* for a different party while counting
continues, and which councils have a majority or are hung.

**Live page:** `https://<owner>.github.io/<repo>/`

How it works

- `poll.py` reads the IEC feed and writes compact JSON (`data.json`, `m/<id>.json`, `map.json`).
- The GitHub Actions workflow runs the poller every 6 minutes and publishes the output to the
  `gh-pages` branch, which GitHub Pages serves together with `index.html`.
- Nothing runs on your own devices; the page just reads the published JSON.

Notes

- "Flip" means a different party from the Nov 2021 winner of the same ward number. By-election
  changes since 2021 are not reflected, and some ward boundaries were redrawn for 2026.
- Winner margins use the ward ballot only. The 2021 comparison uses the IEC's per-ward figures,
  which combine ward and PR ballots.
- Not an IEC product. Data: Electoral Commission of South Africa results feed.
