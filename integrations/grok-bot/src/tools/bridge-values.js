export function scalarText(value, maxChars = Infinity) {
  if (typeof value !== "string") return false;
  let count = 0;
  for (const char of value) {
    const code = char.codePointAt(0);
    if ((code >= 0xd800 && code <= 0xdfff) || ++count > maxChars) return false;
  }
  return true;
}

export function failureText(value) {
  if (!scalarText(value) || !value.trim()) return true;
  const text = value.trim().toLowerCase();
  return /^(model backend error:|lead failed \(|deep lead failed \(|error:|reached max tool steps)/.test(text)
    || /^\[.*? (failed:|ran out of turns)/s.test(text);
}

export function parseReply(text) {
  const out = JSON.parse(text, (key, value) => {
    if (!scalarText(key) || (typeof value === "number" && !Number.isFinite(value))
        || (typeof value === "string" && !scalarText(value))) throw Error("invalid JSON scalar");
    return value;
  });
  if (!out || typeof out !== "object" || Array.isArray(out)) throw Error("expected object");
  return out;
}
