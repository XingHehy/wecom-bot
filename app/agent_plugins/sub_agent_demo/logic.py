def route_score(message_text: str, profile) -> int:
    keywords = profile.route_keywords
    return 4 if any(k in message_text for k in keywords) else 0
