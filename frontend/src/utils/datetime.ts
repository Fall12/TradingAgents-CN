/**
 * 日期时间工具函数
 * 统一处理时间转换和显示
 *
 * 处理逻辑：
 * 1. 若含时区（Z / ±HH:MM），按该时区解析
 * 2. 若无时区（Mongo/多数后端返回的 naive ISO），按 UTC 解析
 * 3. 最终统一显示为北京时间（Asia/Shanghai）
 */

const SHANGHAI = 'Asia/Shanghai'

function normalizeToInstant(dateStr: string | number): Date | null {
  let timeStr: string

  if (typeof dateStr === 'number') {
    const timestamp = dateStr < 10000000000 ? dateStr * 1000 : dateStr
    return new Date(timestamp)
  }

  timeStr = String(dateStr).trim()
  if (!timeStr) return null

  // 空格分隔的 "YYYY-MM-DD HH:mm:ss" → ISO
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(timeStr)) {
    timeStr = timeStr.replace(' ', 'T')
  }

  const hasTimezone =
    timeStr.endsWith('Z') ||
    /[+-]\d{2}:\d{2}$/.test(timeStr) ||
    /[+-]\d{4}$/.test(timeStr)

  // naive ISO → 视为 UTC（Mongo BSON Date 读出后常见）
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(timeStr) && !hasTimezone) {
    timeStr += 'Z'
  }

  const date = new Date(timeStr)
  return isNaN(date.getTime()) ? null : date
}

/**
 * 格式化时间字符串，统一显示为北京时间
 */
export function formatDateTime(
  dateStr: string | number | null | undefined,
  options?: Intl.DateTimeFormatOptions
): string {
  if (dateStr == null || dateStr === '') return '-'

  try {
    const date = normalizeToInstant(dateStr)
    if (!date) {
      console.warn('无效的时间格式:', dateStr)
      return String(dateStr)
    }

    const defaultOptions: Intl.DateTimeFormatOptions = {
      timeZone: SHANGHAI,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    }

    return date.toLocaleString('zh-CN', { ...defaultOptions, ...options })
  } catch (e) {
    console.error('时间格式化错误:', e, dateStr)
    return String(dateStr)
  }
}

/**
 * 格式化时间并添加相对时间描述
 */
export function formatDateTimeWithRelative(dateStr: string | number | null | undefined): string {
  if (dateStr == null || dateStr === '') return '-'

  try {
    const utcDate = normalizeToInstant(dateStr)
    if (!utcDate) {
      console.warn('无效的时间格式:', dateStr)
      return String(dateStr)
    }

    const now = new Date()
    const diff = now.getTime() - utcDate.getTime()
    const days = Math.floor(diff / (1000 * 60 * 60 * 24))
    const hours = Math.floor(diff / (1000 * 60 * 60))
    const minutes = Math.floor(diff / (1000 * 60))

    const formatted = utcDate.toLocaleString('zh-CN', {
      timeZone: SHANGHAI,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    })

    let relative = ''
    if (days > 0) {
      relative = `（${days}天前）`
    } else if (hours > 0) {
      relative = `（${hours}小时前）`
    } else if (minutes > 0) {
      relative = `（${minutes}分钟前）`
    } else {
      relative = '（刚刚）'
    }

    return formatted + ' ' + relative
  } catch (e) {
    console.error('时间格式化错误:', e, dateStr)
    return String(dateStr)
  }
}

export function formatDate(dateStr: string | number | null | undefined): string {
  return formatDateTime(dateStr, {
    timeZone: SHANGHAI,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  })
}

export function formatTime(dateStr: string | number | null | undefined): string {
  return formatDateTime(dateStr, {
    timeZone: SHANGHAI,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  })
}

export function formatRelativeTime(dateStr: string | number | null | undefined): string {
  if (dateStr == null || dateStr === '') return '-'

  try {
    const targetDate = normalizeToInstant(dateStr)
    if (!targetDate) {
      console.warn('无效的时间格式:', dateStr)
      return String(dateStr)
    }

    const now = new Date()
    const diff = targetDate.getTime() - now.getTime()
    const absDiff = Math.abs(diff)

    const seconds = Math.floor(absDiff / 1000)
    const minutes = Math.floor(seconds / 60)
    const hours = Math.floor(minutes / 60)
    const days = Math.floor(hours / 24)
    const isPast = diff < 0

    if (days > 0) {
      return isPast ? `${days}天前` : `${days}天后`
    } else if (hours > 0) {
      return isPast ? `${hours}小时前` : `${hours}小时后`
    } else if (minutes > 0) {
      return isPast ? `${minutes}分钟前` : `${minutes}分钟后`
    } else if (seconds > 10) {
      return isPast ? `${seconds}秒前` : `${seconds}秒后`
    } else {
      return isPast ? '刚刚' : '即将执行'
    }
  } catch (e) {
    console.error('相对时间格式化错误:', e, dateStr)
    return String(dateStr)
  }
}
