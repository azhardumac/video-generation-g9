export function apiErrorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail.map(item => typeof item === 'object' && item && 'msg' in item ? String(item.msg) : '').filter(Boolean)
    if (messages.length) return messages.join('; ')
  }
  return fallback
}
