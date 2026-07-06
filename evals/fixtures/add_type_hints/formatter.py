def format_user(name, age):
    return f"{name} ({age})"


def initials(full_name):
    return "".join(part[0].upper() for part in full_name.split())
