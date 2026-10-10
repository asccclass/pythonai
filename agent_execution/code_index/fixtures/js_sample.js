export class Service {
  calculate(value) { return value + 1; }
}
export function calculate(value) { return value * 2; }
const result = calculate(3);
const ignored = "calculate"; // calculate is a comment, not a reference
