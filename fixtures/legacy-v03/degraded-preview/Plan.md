# Plan

Status: active

| Task | Description | Status | Mode | Files | Depends | Verify | Evidence |
|---|---|---|---|---|---|---|---|
| SF-001 | Build the static preview | complete | solo | public/index.html | - | python3 -m unittest tests.test_preview | public/index.html |
| AMEND-1 | Recheck the fallback preview | complete | solo | public/index.html | SF-001 | python3 -m unittest tests.test_preview | .starforge/live/AMEND-1/preview/manifest.json |
