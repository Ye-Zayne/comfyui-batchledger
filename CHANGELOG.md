# Changelog

## 0.1.0

- Four ComfyUI V1 nodes: Plan, Next Image, Verified Save, and Report.
- Folder/JSON/CSV inputs with stable seeds and content-addressed item identities.
- SQLite result ledger, verified atomic PNG saves, output corruption detection, and selective reruns.
- Server-side automatic batching and limited upstream-error retries without requiring a WebSocket.
- User cancellation stops automatic continuation; lazy inputs avoid processing completed records.
- Input/output root and symlink containment checks, and stale input/plan checks before commit.
- English/Chinese documentation, runnable API examples, 39 unit tests, and real ComfyUI 0.33.1 smoke validation.
