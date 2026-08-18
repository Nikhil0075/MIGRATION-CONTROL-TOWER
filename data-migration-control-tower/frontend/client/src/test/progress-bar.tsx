import { h } from "preact";

export function ProgressBar({ value = -1, max = 100, ...props }: any) {
  const indeterminate = value < 0;
  return (
    <div
      role="progressbar"
      aria-valuemin={indeterminate ? undefined : 0}
      aria-valuemax={indeterminate ? undefined : max}
      aria-valuenow={indeterminate ? undefined : value}
      {...props}
    />
  );
}
