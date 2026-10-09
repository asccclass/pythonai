from agent_execution import policy_tool, resolve_path

@policy_tool("get_weather_test")
def get_weather_test(location: str) -> str:
    return f"The weather in {location} is sunny and 25°C!"
