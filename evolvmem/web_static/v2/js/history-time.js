/* Display source record times without inferring a timezone or inventing a date. */
(function (root) {
  const E = root.EvolvMemV2;
  const shanghai = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  E.formatHistoryTime = function (value) {
    if (typeof value !== 'string' || !value.trim()) return '时间未知';
    const raw = value.trim();
    const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?(Z|[+-]\d{2}:?\d{2})?$/i.exec(raw);
    if (!match) return '时间未知';
    const [, ys, ms, ds, hs, mins, ss, fraction, zone] = match;
    const year = Number(ys), month = Number(ms), day = Number(ds);
    const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1] || Number(hs || 0) > 23 || Number(mins || 0) > 59 || Number(ss || 0) > 59 || (zone && hs == null)) return '时间未知';
    if (!zone) return raw.replace('T', ' ') + '（来源时区未注明）';
    const compact = zone.replace(':', '').toUpperCase();
    if (compact !== 'Z' && (Number(compact.slice(1, 3)) > 23 || Number(compact.slice(3, 5)) > 59)) return '时间未知';
    const offset = compact === 'Z' ? 'Z' : compact.slice(0, 3) + ':' + compact.slice(3);
    const iso = ys + '-' + ms + '-' + ds + 'T' + hs + ':' + mins + ':' + (ss || '00') + (fraction ? '.' + fraction.slice(0, 3).padEnd(3, '0') : '') + offset;
    const date = new Date(iso);
    if (!Number.isFinite(date.getTime())) return '时间未知';
    const parts = Object.fromEntries(shanghai.formatToParts(date).map(p => [p.type, p.value]));
    return parts.year + '-' + parts.month + '-' + parts.day + ' ' + parts.hour + ':' + parts.minute;
  };
})(typeof window !== 'undefined' ? window : globalThis);
