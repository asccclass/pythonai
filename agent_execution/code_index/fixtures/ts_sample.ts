export interface Result { value: number; }
export class Service {
  calculate(value: number): number { return value + 1; }
}
export const calculate = (value: number): number => value * 2;
const label = "😀"; calculate(3);
