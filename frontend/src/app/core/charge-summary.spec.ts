import { lockLine } from './charge-summary';

describe('lockLine', () => {
  const now = Date.parse('2026-11-10T12:00:00Z');

  it('says until when the plan follows the forecast', () => {
    const line = lockLine({ locks_at: '2026-11-10T23:20:00Z', locked_at: null }, 'UTC', now);
    expect(line).toEqual({
      locked: false,
      text: 'Updates with the forecast until 23:20, then locks.',
    });
  });

  it('names the day when the lock is not today', () => {
    const line = lockLine({ locks_at: '2026-11-11T23:20:00Z', locked_at: null }, 'UTC', now);
    expect(line?.text).toContain('23:20 Wednesday');
  });

  it('says when it locked', () => {
    const line = lockLine(
      { locks_at: '2026-11-10T21:00:00Z', locked_at: '2026-11-10T21:00:30Z' },
      'Europe/London',
      now,
    );
    expect(line).toEqual({ locked: true, text: "Locked at 21:00. This plan won't change now." });
  });

  it('says nothing for a plan without lock times', () => {
    expect(lockLine({}, 'UTC', now)).toBeNull();
    expect(lockLine(null, 'UTC', now)).toBeNull();
  });
});
