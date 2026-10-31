import { HttpErrorResponse } from '@angular/common/http';

/** Turns a FastAPI error response into one readable line. */
export function errorMessage(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    const detail = err.error?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail.map((d) => `${d.loc?.slice(1).join(' › ')}: ${d.msg}`).join('; ');
    }
    if (err.status === 0) return 'Cannot reach the server.';
  }
  return 'Something went wrong. Please try again.';
}
