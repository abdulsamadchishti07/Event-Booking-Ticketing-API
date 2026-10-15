import http from 'k6/http';
import { check, sleep } from 'k6';

// ============================================================================
// k6 Load Test Configuration: User Capacity & Concurrency Ramp-Up
// ============================================================================
export const options = {
  stages: [
    { duration: '10s', target: 50 },   // Stage 1: Warm up with 50 concurrent virtual users
    { duration: '20s', target: 200 },  // Stage 2: Push to 200 concurrent users (capacity testing)
    { duration: '10s', target: 500 },  // Stage 3: Stress spike to 500 concurrent users
    { duration: '10s', target: 0 },    // Stage 4: Ramp down back to 0
  ],
  thresholds: {
    // 95% of cached requests should complete in under 50ms
    http_req_duration: ['p(95)<50'],
    // HTTP error rate must remain under 1%
    http_req_failed: ['rate<0.01'],
  },
};

// Target URL defaults to Nginx (port 80) or direct Uvicorn (port 8000)
const BASE_URL = __ENV.TARGET_URL || 'http://localhost';

export default function () {
  // Test Event Discovery endpoint (exercises Redis cache read-through)
  const res = http.get(`${BASE_URL}/services?limit=20`);

  check(res, {
    'status is 200': (r) => r.status === 200,
    'served from cache (HIT)': (r) => r.headers['X-Cache'] === 'HIT',
    'fast response (<25ms)': (r) => r.timings.duration < 25,
  });

  // Short user think time between actions (100ms)
  sleep(0.1);
}
