def route_score(message_text: str, profile) -> int:
    return 6 if any(k in message_text for k in profile.route_keywords) else 0

