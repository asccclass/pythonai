package demo

type Service struct{}
func (s Service) Calculate(value int) int { return value + 1 }
func Calculate(value int) int { return value * 2 }
func Use() int { return Calculate(3) }
