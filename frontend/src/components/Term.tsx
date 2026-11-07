import { useId, useState } from "react";
import { GLOSSARY } from "../plain";

/** A word with a small "?" that explains it in plain language. */
export default function Term({ k, children }: { k: keyof typeof GLOSSARY | string; children?: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const text = GLOSSARY[k as string];
  if (!text) return <>{children ?? k}</>;
  return (
    <span className="term" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      {children ?? k}
      <button type="button" className="term-q" aria-label={`What is ${k}?`} aria-expanded={open}
        aria-describedby={open ? id : undefined} onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }} onBlur={() => setOpen(false)}>?</button>
      {open && <span id={id} className="term-pop" role="tooltip"><strong>{k}</strong>{text}</span>}
    </span>
  );
}
