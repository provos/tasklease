import type { Scopes } from "./generated/protocol";

/** Match a single segment without constructing a regular expression. */
function segmentMatches(pattern: string, value: string): boolean {
  let p = 0;
  let v = 0;
  let star = -1;
  let restart = 0;
  while (v < value.length) {
    if (pattern[p] === "*") {
      star = p++;
      restart = v;
    } else if (pattern[p] === value[v]) {
      p++;
      v++;
    } else if (star !== -1) {
      p = star + 1;
      v = ++restart;
    } else {
      return false;
    }
  }
  while (pattern[p] === "*") p++;
  return p === pattern.length;
}

/** Whole-topic glob matching; ** consumes at least one whole segment. */
export function matches(glob: string, topic: string): boolean {
  const patterns = glob.split("/");
  const segments = topic.split("/");
  if (segments.some((segment) => !segment)) return false;
  let previous = Array<boolean>(segments.length + 1).fill(false);
  previous[0] = true;
  for (const pattern of patterns) {
    if (!pattern || (pattern !== "**" && pattern.includes("**"))) return false;
    const current = Array<boolean>(segments.length + 1).fill(false);
    for (let index = 1; index <= segments.length; index++) {
      current[index] =
        pattern === "**"
          ? previous[index - 1]! || current[index - 1]!
          : previous[index - 1]! &&
            segmentMatches(pattern, segments[index - 1]!);
    }
    previous = current;
  }
  return previous[segments.length]!;
}

export function allows(
  scopes: Scopes,
  kind: keyof Scopes,
  topic: string,
): boolean {
  return scopes[kind].some((glob) => matches(glob, topic));
}
