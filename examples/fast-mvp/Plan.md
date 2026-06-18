# Plan.md

## Task Ledger

| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |
|------|-------------|--------|------|-------|---------|--------|----------|
| MVP-001 | Create habit list UI and empty state. | ready | delegate | public/index.html, public/app.js | - | npm test | - |
| MVP-002 | Add habit creation and completion behavior. | queued | delegate | public/app.js, src/state.js | MVP-001 | npm test | - |
| MVP-003 | Visual verification on desktop and mobile. | queued | solo | - | MVP-002 | browser-run with desktop and mobile viewports | - |
