import { useId, useState, type ButtonHTMLAttributes, type HTMLAttributes, type InputHTMLAttributes, type ReactNode } from "react";

import styles from "./ui.module.css";

const classes = (...values: Array<string | false | null | undefined>) => values.filter(Boolean).join(" ");

type PageShellProps = HTMLAttributes<HTMLDivElement> & { eyebrow?: string; title?: string; description?: string; actions?: ReactNode };

export function PageShell({ eyebrow, title, description, actions, children, className, ...props }: PageShellProps) {
  return <div className={classes(styles.pageShell, className)} {...props}>
    {(eyebrow || title || description || actions) && <header className={styles.pageHeader}><div>
      {eyebrow && <p className={styles.eyebrow}>{eyebrow}</p>}
      {title && <h1>{title}</h1>}
      {description && <p className={styles.description}>{description}</p>}
    </div>{actions && <div className={styles.actions}>{actions}</div>}</header>}
    {children}
  </div>;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "ghost" };
export function Button({ variant = "primary", className, type = "button", ...props }: ButtonProps) {
  return <button className={classes(styles.button, styles[`button_${variant}`], className)} type={type} {...props} />;
}

type FieldProps = InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string; error?: string };
export function Field({ label, hint, error, className, id, ...props }: FieldProps) {
  const generatedId = useId();
  const inputId = id || generatedId;
  const messageId = `${inputId}-message`;
  return <label className={styles.field} htmlFor={inputId}><span>{label}</span>
    <input aria-describedby={hint || error ? messageId : undefined} aria-invalid={Boolean(error)} className={classes(styles.input, className)} id={inputId} {...props} />
    {(error || hint) && <small className={error ? styles.fieldError : styles.fieldHint} id={messageId}>{error || hint}</small>}
  </label>;
}

export function Surface({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={classes(styles.surface, className)} {...props} />;
}

type StatusBadgeProps = HTMLAttributes<HTMLSpanElement> & { tone?: "neutral" | "success" | "warning" | "danger" | "info" };
export function StatusBadge({ tone = "neutral", className, ...props }: StatusBadgeProps) {
  return <span className={classes(styles.badge, styles[`badge_${tone}`], className)} {...props} />;
}

export function InfoTooltip({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span className={styles.tooltipWrap}>
      <button
        aria-describedby={open ? id : undefined}
        aria-label={`Пояснение: ${label}`}
        className={styles.tooltipTrigger}
        onBlur={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        type="button"
      >
        ?
      </button>
      {open && <span className={styles.tooltipBubble} id={id} role="tooltip">{children}</span>}
    </span>
  );
}

export type MetricTone = "neutral" | "success" | "warning" | "danger" | "info";
export type MetricDelta = { text: string; direction: "up" | "down" | "flat"; isFavorable: boolean | null };

const DELTA_ARROW: Record<MetricDelta["direction"], string> = { up: "▲", down: "▼", flat: "•" };

type MetricCardProps = {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: MetricTone;
  delta?: MetricDelta;
  tooltip?: ReactNode;
  className?: string;
};

export function MetricCard({ label, value, hint, tone = "neutral", delta, tooltip, className }: MetricCardProps) {
  const deltaTone = delta ? (delta.isFavorable === null ? "neutral" : delta.isFavorable ? "success" : "danger") : null;
  return (
    <Surface className={classes(styles.metric, styles[`metric_${tone}`], className)}>
      <span className={styles.metricLabel}>
        {label}
        {tooltip && <InfoTooltip label={label}>{tooltip}</InfoTooltip>}
      </span>
      <strong>{value}</strong>
      {delta && (
        <span className={classes(styles.delta, styles[`delta_${deltaTone}`])}>
          {DELTA_ARROW[delta.direction]} {delta.text}
        </span>
      )}
      {hint && <small>{hint}</small>}
    </Surface>
  );
}

type StateProps = { title: string; description?: string; actionLabel?: string; onAction?: () => void };
function StatePanel({ kind, title, description, actionLabel, onAction }: StateProps & { kind: "empty" | "loading" | "error" }) {
  return <Surface aria-live={kind === "error" ? "assertive" : "polite"} className={classes(styles.state, styles[`state_${kind}`])} role={kind === "error" ? "alert" : "status"}>
    {kind === "loading" && <span aria-hidden="true" className={styles.spinner} />}
    <strong>{title}</strong>{description && <p>{description}</p>}
    {actionLabel && onAction && <Button onClick={onAction} variant={kind === "error" ? "secondary" : "primary"}>{actionLabel}</Button>}
  </Surface>;
}

export const EmptyState = (props: StateProps) => <StatePanel kind="empty" {...props} />;
export const LoadingState = (props: StateProps) => <StatePanel kind="loading" {...props} />;
export const ErrorState = (props: StateProps) => <StatePanel kind="error" {...props} />;
