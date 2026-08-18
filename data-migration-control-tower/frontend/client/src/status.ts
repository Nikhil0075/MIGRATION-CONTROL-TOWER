export function statusTone(value: unknown): string {
  const status = String(value || "UNKNOWN").toUpperCase();
  if (["HEALTHY", "COMPLETE", "COMPLETED", "PASSED", "APPROVED", "ALLOW", "OPEN", "RESOLVED", "OBSERVED"].includes(status)) return "success";
  if (["FAILED", "DENY", "DEGRADED", "CRITICAL", "BLOCKED", "PUBLISH_FAILED"].includes(status)) return "danger";
  if (["MIGRATING", "VALIDATING", "INVESTIGATING", "REMEDIATING", "HOLD", "HELD", "PENDING", "QUEUED", "PUBLISHED"].includes(status)) return "warning";
  return "neutral";
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not available";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return new Intl.NumberFormat().format(value);
  if (typeof value === "object") return JSON.stringify(value);
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}T/.test(text)) {
    const date = new Date(text);
    if (!Number.isNaN(date.valueOf())) return date.toLocaleString();
  }
  return text;
}
