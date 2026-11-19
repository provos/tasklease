export function dropCompletionLatencyColumns(sql: SqlStorage): void {
  sql.exec(`ALTER TABLE tasks DROP COLUMN first_owned_at;
    ALTER TABLE stats_hourly DROP COLUMN completion_wait_ms_sum;
    ALTER TABLE stats_hourly DROP COLUMN completion_after_claim_ms_sum;
    ALTER TABLE stats_hourly DROP COLUMN completion_count;`);
}
