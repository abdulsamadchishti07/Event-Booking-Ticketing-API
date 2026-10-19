import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '15s', target: 20 },   // Warm-up / low load
    { duration: '15s', target: 50 },   // Moderate load
    { duration: '15s', target: 100 },  // High load baseline
    { duration: '15s', target: 200 },  // Peak load baseline
    { duration: '10s', target: 0 },    // Ramp-down
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],    // Error rate must be under 1%
    http_req_duration: ['p(95)<100'],  // p95 latency under 100ms
  },
};

export default function () {
  const target = __ENV.TARGET || 'http://localhost';
  const ipSuffix = (__VU % 250) + 1;
  const headers = {
    'X-Forwarded-For': `198.51.100.${ipSuffix}`,
  };

  // 1. Browse event listings (Discovery)
  const listRes = http.get(`${target}/services?limit=10`, { headers });
  check(listRes, {
    'GET /services status 200': (r) => r.status === 200,
  });

  // Short human-like pause between clicks
  sleep(0.5);
}
