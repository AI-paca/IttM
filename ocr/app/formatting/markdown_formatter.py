import re


class MarkdownFormatter:
    @staticmethod
    def format_text(text: str) -> str:
        if not text.strip():
            return ""

        # Normalize bullets and list symbols to standard hyphens
        # Replace bullets: •, ·, ●
        text = re.sub(r"[•·●]", "-", text)
        # Replace long dashes (em-dash, en-dash, minus) with normal hyphen
        text = re.sub(r"[—–−]", "-", text)

        lines = text.split("\n")
        formatted_lines = []
        in_list = False

        for line in lines:
            stripped = line.strip()

            # Skip empty lines, handled later
            if not stripped:
                if formatted_lines and formatted_lines[-1].strip():
                    formatted_lines.append("")
                continue

            # Keep + and * as list markers only if at the beginning of the line
            is_valid_marker_char = len(stripped) > 0 and stripped[0] in ["-", "+", "*"]
            is_followed_by_space = len(stripped) > 1 and stripped[1] == " "

            if is_valid_marker_char and is_followed_by_space:
                in_list = True
                # Normalize the marker to a standard dash for lists if we are fixing the format
                formatted_lines.append(f"- {stripped[1:].strip()}")
                continue

            # Check if line starts with a number or dot (possible list)
            if stripped and len(stripped) > 2 and stripped[1:2] == "." and stripped[0].isdigit():
                in_list = True
                formatted_lines.append(f"- {stripped[2:].strip()}")
                continue

            # If not a list item
            if in_list and formatted_lines and formatted_lines[-1].strip().startswith("-"):
                # formatted_lines.append("")  # optional separator, usually not strictly required
                in_list = False
            formatted_lines.append(stripped)

        # Remove extra empty lines
        result = []
        prev_empty = False
        for line in formatted_lines:
            is_empty = not line.strip()
            if not (is_empty and prev_empty):
                result.append(line)
            prev_empty = is_empty

        # Trim empties at edges
        while result and not result[0].strip():
            result.pop(0)
        while result and not result[-1].strip():
            result.pop()

        return "\n".join(result)
