-- Story 38.12: SAV is a first-class governed tabular format.
BEGIN;

ALTER TABLE app.managed_feed_import_ledger
    DROP CONSTRAINT IF EXISTS managed_feed_import_ledger_feed_format_check;
ALTER TABLE app.managed_feed_import_ledger
    ADD CONSTRAINT managed_feed_import_ledger_feed_format_check
    CHECK (feed_format IN ('csv', 'excel', 'sav', 'google_sheets'));

ALTER TABLE app.csv_excel_import_contracts
    DROP CONSTRAINT IF EXISTS csv_excel_import_contracts_format_check;
ALTER TABLE app.csv_excel_import_contracts
    ADD CONSTRAINT csv_excel_import_contracts_format_check
    CHECK (format IN ('csv', 'excel', 'sav'));

COMMENT ON CONSTRAINT managed_feed_import_ledger_feed_format_check
    ON app.managed_feed_import_ledger IS
    'Closed durable vocabulary for governed tabular and Google Sheets imports.';
COMMENT ON CONSTRAINT csv_excel_import_contracts_format_check
    ON app.csv_excel_import_contracts IS
    'One immutable confirmed parser contract may select CSV, Excel, or SAV.';

COMMIT;
