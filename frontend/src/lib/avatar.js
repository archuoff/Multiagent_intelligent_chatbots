const AVATAR_PALETTE = ["#2563eb", "#7c3aed", "#0891b2", "#c2410c", "#059669", "#be185d"];

export function initials(name) {
  const words = (name || "").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

export function avatarColor(agentId) {
  let hash = 0;
  for (let i = 0; i < agentId.length; i++) hash = (hash * 31 + agentId.charCodeAt(i)) >>> 0;
  return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
}
