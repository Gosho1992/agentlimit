import { useState } from "react";
import { GLOSSARY } from "../plain";

/** A word with a small "?" that explains it in plain language. */
export default function Term({ k, children }: { k: keyof typeof GLOSSARY | string; children?: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const text = GLOSSARY[k as string];
  if (!text) return <>{children ?? k}</>;
  return (
    <span className="term" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      {children ?? k}
      <button type="button" className="term-q" aria-label={`What is ${k}?`} onClick={() => setOpen((o) => !o)}>?</button>
      {open && <span className="term-pop" role="tooltip"><strong>{k}</strong>{text}</span>}
    </span>
  );
}
