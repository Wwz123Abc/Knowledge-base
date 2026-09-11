import http from "k6/http";
import { check, sleep } from "k6";

const peakVus = Number.parseInt(__ENV.PEAK_VUS || "30", 10);
const rampUp = __ENV.RAMP_UP || "30s";
const hold = __ENV.HOLD || "1m";
const rampDown = __ENV.RAMP_DOWN || "30s";
const p95LimitMs = Number.parseInt(__ENV.P95_LIMIT_MS || "5000", 10);

export const options = {
  stages: [
    { duration: rampUp, target: Math.min(10, peakVus) },
    { duration: hold, target: peakVus },
    { duration: rampDown, target: 0 },
  ],
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: [`p(95)<${p95LimitMs}`],
  },
};

export default function () {
  const baseUrl = __ENV.BASE_URL || "http://localhost:8000";
  const response = http.post(
    `${baseUrl}/api/chat`,
    JSON.stringify({ question: "员工申请年假需要提前多久？" }),
    { headers: { "Content-Type": "application/json" } },
  );
  check(response, { "chat returns success": (r) => r.status === 200 });
  sleep(1);
}
