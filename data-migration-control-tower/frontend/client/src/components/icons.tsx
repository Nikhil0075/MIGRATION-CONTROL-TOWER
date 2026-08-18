import { h } from "preact";

const PATHS: Record<string, string[]> = {
  menu: ["M4 7h16M4 12h16M4 17h16"],
  overview: ["M4 13h6V4H4v9Zm10 7h6V11h-6v9ZM4 20h6v-3H4v3Zm10-13h6V4h-6v3Z"],
  estates: ["M4 7h16v10H4z", "M8 7V4h8v3M8 17v3m8-3v3"],
  assessments: ["M5 4h14v16H5z", "m8 8 2 2 4-4M8 15h8"],
  waves: ["M3 8c3-3 6 3 9 0s6 3 9 0M3 16c3-3 6 3 9 0s6 3 9 0"],
  runs: ["M5 4h14v16H5z", "M9 9h6M9 13h6M9 17h4"],
  lineage: ["M6 5h5v5H6zM13 14h5v5h-5zM8.5 10v3h7v1"],
  reconciliation: ["M4 7h11M4 12h16M4 17h11", "m16 5 3 2-3 2m0 6 3 2-3 2"],
  policies: ["M12 3 20 7v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z", "m9 12 2 2 4-4"],
  agents: ["M8 9a4 4 0 1 0 8 0 4 4 0 0 0-8 0ZM4 21a8 8 0 0 1 16 0"],
  evaluations: ["M4 19V9m5 10V5m5 14v-7m5 7V3"],
  health: ["M3 12h4l2-5 4 10 2-5h6"],
  search: ["m20 20-4.5-4.5", "M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15Z"],
  alert: ["M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"],
  user: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-8 9a8 8 0 0 1 16 0"],
  close: ["M5 5l14 14M19 5 5 19"],
  chevron: ["m9 18 6-6-6-6"],
  refresh: ["M20 6v5h-5", "M4 18v-5h5", "M6.1 9A7 7 0 0 1 18 6l2 5M4 13l2 5a7 7 0 0 0 11.9-3"],
  external: ["M14 4h6v6M20 4l-9 9", "M18 13v7H4V6h7"],
};

export function Icon({ name, size = 18, label }: { name: string; size?: number; label?: string }) {
  return (
    <svg
      class="mct-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : "true"}
    >
      {(PATHS[name] || PATHS.overview).map((path) => <path d={path} />)}
    </svg>
  );
}
