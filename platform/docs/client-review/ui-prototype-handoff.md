# UI prototype transfer

The previous UI work is available at `/ui-preview`, linked as **UI prototype** from the live studio sidebar. The prototype has a **Back to live studio** link.

The new repository already has backend-connected chat, account login, persisted conversations, generation, and dataset previews. Its `routes/home.tsx`, backend, authentication, and package manifests were preserved. The old homepage was adapted as `routes/ui-preview.tsx` instead of overwriting the live homepage.

## Included

- Optional source connector and CSV-only preview flow.
- PostgreSQL, MySQL, and Oracle connection profiles.
- Attachment input alongside the connector.
- Healthcare requirements and saved run snapshots.
- Schema preview including 50 tables with 200 columns each.
- CSV preview/download, independent export destination, run search and filters.
- Sign-in navigation uses the existing real `/login` page; simulated account screens have been removed.

The prototype's database operations, generation, and export operations remain simulated. Its local storage keys use the `syda-ui-preview-` prefix and do not share the live studio's conversations or authentication. No dependency or backend change was needed for the transfer.

## Files to review

- `platform/frontend/app/routes/ui-preview.tsx` — transferred prototype homepage.
- `platform/frontend/app/components/database-workspace.tsx`
- `platform/frontend/app/components/review-workspace.tsx`
- `platform/frontend/app/components/scenario-preview.tsx`
- `platform/frontend/app/routes.ts` — registers the preview route.
- `platform/frontend/app/components/studio/app-shell.tsx` — links to the prototype.

## Next integration work

Use the prototype to agree on interactions, then adapt selected components to the live API contracts. Keep the existing real authentication, job state, conversation persistence, schema editor, and downloadable results. Do not replace live functionality with the mock services from this prototype.

## Local verification

From `platform/frontend`, install the locked dependencies with `npm ci`, then run `npm run typecheck` and `npm run build`. Start with `npm run dev` and open `/ui-preview`. The prototype can be reviewed without a backend; `/app` still uses the normal authentication and backend flow.

## Repository compatibility fixes

The new checkout was missing `app/lib/studio-types.ts`, although its live screens import it. DTOs were restored from the backend response models and frontend call sites. `.gitignore` now permits this frontend source folder so the types are included in commits. The two imported UI helpers use the new repo's existing `cn` package.

Additional files: `platform/frontend/app/lib/studio-types.ts`, `platform/frontend/app/components/ui/avatar.tsx`, and `platform/frontend/app/components/ui/dropdown-menu.tsx`.

Use Node 22.22 or newer; the installed React Router 8.3.1 packages require it. Verification used the bundled Node 24 runtime. The system Node 22.17 is older than the package requirement.

## Live studio integration update

The live chat now supports schema-only database inspection through authenticated `POST /api/connections/inspect`, using Syda's `DatabaseSchemaLoader`. Users can keep multiple connection profiles during a chat-page session, test/retry connections, search tables and columns, and send imported structure with their prompt. Credentials are not persisted or included in AI prompts. Database drivers must be available on the backend (PostgreSQL: psycopg2; MySQL: pymysql; Oracle: oracledb). Imported schema is sent to the configured AI provider; generation still requires reviewing its scenario plan. CSV output is supported; live database write-back is not implemented.

Review dialogs show the complete rule list, table definitions, and CSV destination. Chat results and run evaluation show actual table counts and reported validation failures. Requested scenario instances are distinguished from total generated rows because workflow paths can create different counts per table.

Composer controls use matching 36px heights and wrap at narrow widths. Existing authentication is retained. The separate UI preview remains available for features still awaiting integration.

Validation: production build and TypeScript checks; browser checks with controlled API responses; isolated schema-endpoint tests with a real SQLite schema and foreign keys. Actual PostgreSQL/MySQL/Oracle connections and AI-provider generation require the team's running backend and credentials.
