import { useCallback, useEffect, useState } from "react";
import type { Action, SchemaWarning } from "../api/store";
import { projectStore } from "../api/controlPlane";

// Schema-warning banner. The Cypher UI surfaces backend-reported
// schema introspection warnings (e.g. ANALYZER_NOT_INSTALLED) here.
// The SPARQL backend does not introspect database schemas — its
// schema input is the user-supplied OWL/Turtle ontology — so this
// component is included for layout symmetry but renders nothing
// today. A future ontology-validation endpoint can populate
// `warnings` with the same shape and reuse the dismissal storage.

// One SchemaWarning shape for the whole UI (the store owns it).
export type { SchemaWarning };

interface Props {
  warnings: SchemaWarning[];
  url: string;
  database: string;
  // Kept in the prop signature so App.tsx can pass these without a
  // shape change once a SPARQL-side validation endpoint exists.
  token?: string | null;
  dispatch?: (action: Action) => void;
}

const DISMISSED_KEY = "schema_warning_dismissed";

function loadDismissed(): Record<string, number> {
  try {
    const { storage, key } = projectStore(DISMISSED_KEY);
    const raw = storage.getItem(key);
    return raw ? (JSON.parse(raw) as Record<string, number>) : {};
  } catch {
    return {};
  }
}

function saveDismissed(map: Record<string, number>) {
  try {
    const { storage, key } = projectStore(DISMISSED_KEY);
    storage.setItem(key, JSON.stringify(map));
  } catch {
    /* localStorage may be unavailable */
  }
}

function dismissalKey(url: string, database: string, code: string): string {
  return `${url}::${database}::${code}`;
}

export default function SchemaWarningBanner({
  warnings,
  url,
  database,
}: Props) {
  const [dismissed, setDismissed] = useState<Record<string, number>>(() =>
    loadDismissed(),
  );

  useEffect(() => {
    setDismissed(loadDismissed());
  }, [url, database]);

  // Info notes describe normal operation (e.g. ANALYZER_BASELINE_NO_LLM on
  // every analyzer schema) — not something to warn about. cypher-py parity.
  const visible = warnings.filter(
    (w) => w.severity !== "info" && !dismissed[dismissalKey(url, database, w.code)],
  );

  const handleDismiss = useCallback(
    (code: string) => {
      const next = {
        ...dismissed,
        [dismissalKey(url, database, code)]: Date.now(),
      };
      setDismissed(next);
      saveDismissed(next);
    },
    [dismissed, url, database],
  );

  if (visible.length === 0) return null;

  return (
    <div className="bg-amber-900/20 border-b border-amber-800/30">
      {visible.map((w, i) => (
        <div
          key={`${w.code}-${i}`}
          className="px-4 py-1.5 flex items-start gap-2.5 border-t border-amber-800/20 first:border-t-0"
        >
          <span className="text-amber-500 text-xs mt-0.5 shrink-0">
            &#9888;
          </span>
          <div className="flex-1 min-w-0">
            <span className="text-xs text-amber-300 font-medium">{w.code}</span>
            <span className="text-xs text-amber-400/90 ml-2">{w.message}</span>
            {w.install_hint && (
              <span className="text-xs text-amber-400/70 ml-2">
                Hint:{" "}
                <code className="bg-amber-950/40 px-1 py-0.5 rounded">
                  {w.install_hint}
                </code>
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={() => handleDismiss(w.code)}
            className="px-1.5 py-0.5 text-[11px] rounded text-amber-400/70 hover:text-amber-200 hover:bg-amber-800/30 transition-colors shrink-0"
          >
            Dismiss
          </button>
        </div>
      ))}
    </div>
  );
}
