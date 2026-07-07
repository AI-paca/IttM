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

        for line in lines:
            stripped = line.strip()

            # Skip empty lines, handled later
            if not stripped:
                if formatted_lines and formatted_lines[-1].strip():
                    formatted_lines.append("")
                continue

            if MarkdownFormatter._is_empty_control_marker(stripped):
                continue

            split_lines = MarkdownFormatter._split_inline_ordered_list(stripped)
            if len(split_lines) > 1:
                for split_line in split_lines:
                    formatted_lines.extend(
                        MarkdownFormatter._format_single_line(split_line)
                    )
                continue

            formatted_lines.extend(MarkdownFormatter._format_single_line(stripped))

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

        return MarkdownFormatter._fence_pipe_art_blocks("\n".join(result))

    @staticmethod
    def _format_single_line(stripped: str) -> list[str]:
        if MarkdownFormatter._is_empty_control_marker(stripped):
            return []

        formatted_lines = []

        # Keep + and * as list markers only if at the beginning of the line
        is_valid_marker_char = len(stripped) > 0 and stripped[0] in ["-", "+", "*"]
        is_followed_by_space = len(stripped) > 1 and stripped[1] == " "

        if is_valid_marker_char and is_followed_by_space:
            # Normalize the marker to a standard dash for lists if we are fixing the format
            formatted_lines.append(f"- {stripped[1:].strip()}")
            return formatted_lines

        numbered = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if numbered:
            formatted_lines.append(
                f"{numbered.group(1)}. {numbered.group(2).strip()}"
            )
            return formatted_lines

        formatted_lines.append(stripped)
        return formatted_lines

    @staticmethod
    def _is_empty_control_marker(line: str) -> bool:
        return bool(re.fullmatch(r"#{1,6}|[-+*]", line.strip()))

    @staticmethod
    def _split_inline_ordered_list(line: str) -> list[str]:
        if "|" in line:
            return [line]
        matches = list(re.finditer(r"(^|(?<=\s))(\d{1,2})[.)]\s+", line))
        if len(matches) < 2:
            return [line]
        numbers = [int(match.group(2)) for match in matches]
        if any(numbers[index] + 1 != numbers[index + 1] for index in range(len(numbers) - 1)):
            return [line]

        prefix = line[: matches[0].start()].strip()
        if prefix and not prefix.endswith(":"):
            return [line]

        result: list[str] = []
        if prefix:
            result.append(prefix)
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            body = line[match.end() : end].strip()
            if body:
                result.append(f"{match.group(2)}. {body}")
        return result if len(result) > 1 else [line]

    @staticmethod
    def _is_pipe_row(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2

    @staticmethod
    def _is_separator_row(line: str) -> bool:
        stripped = line.strip()
        if not MarkdownFormatter._is_pipe_row(stripped):
            return False
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)

    @staticmethod
    def _is_pipe_art_connector_line(line: str) -> bool:
        compact = line.strip().replace(" ", "")
        if not compact or len(compact) > 12:
            return False
        if re.search(r"[-_=+|/\\<>()[\]{}]", compact):
            return bool(re.fullmatch(r"[-_=+|/\\<>()[\]{}.,:;`'\"A-Za-zА-Яа-я]+", compact))
        if re.fullmatch(r"[LlГгТтИиЯя]+", compact):
            return True
        return len(compact) <= 8 and compact.isupper()

    @staticmethod
    def _is_pipe_art_candidate_line(line: str) -> bool:
        stripped = line.strip()
        return (
            MarkdownFormatter._is_pipe_row(stripped)
            or MarkdownFormatter._is_pipe_art_connector_line(stripped)
        )

    @staticmethod
    def _looks_like_pipe_art_group(group: list[str]) -> bool:
        pipe_rows = [
            line
            for line in group
            if MarkdownFormatter._is_pipe_row(line)
        ]
        if len(pipe_rows) < 2:
            return False
        if any(MarkdownFormatter._is_separator_row(line) for line in group):
            return False
        connector_count = sum(
            1
            for line in group
            if MarkdownFormatter._is_pipe_art_connector_line(line)
        )
        return (
            any("->" in line or "→" in line for line in group)
            or any(re.search(r"\|\s*\|", line) for line in pipe_rows)
            or connector_count >= 2
        )

    @staticmethod
    def _fence_pipe_art_blocks(text: str) -> str:
        fenced: list[str] = []
        group: list[str] = []
        in_fence = False

        def flush_group() -> None:
            nonlocal group
            if not group:
                return
            if MarkdownFormatter._looks_like_pipe_art_group(group):
                fenced.extend(("```text", *group, "```"))
            else:
                fenced.extend(group)
            group = []

        for line in text.splitlines():
            if line.strip().startswith("```"):
                flush_group()
                fenced.append(line)
                in_fence = not in_fence
                continue
            if in_fence:
                fenced.append(line)
                continue
            if not line.strip():
                flush_group()
                fenced.append(line)
                continue
            if MarkdownFormatter._is_pipe_art_candidate_line(line):
                group.append(line)
                continue
            flush_group()
            fenced.append(line)
        flush_group()
        return "\n".join(fenced)

    @staticmethod
    def _split_pipe_row(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    @staticmethod
    def _format_pipe_row(cells: list[str], column_count: int) -> str:
        padded = [*cells, *([""] * max(0, column_count - len(cells)))]
        return "| " + " | ".join(padded[:column_count]) + " |"

    @staticmethod
    def _repair_pipe_table_group(group: list[str]) -> list[str]:
        if len(group) < 2:
            return group
        rows = [MarkdownFormatter._split_pipe_row(line) for line in group]
        column_count = max(len(row) for row in rows)
        repaired = []
        separator_seen = False
        for index, line in enumerate(group):
            if MarkdownFormatter._is_separator_row(line):
                separator_seen = True
                repaired.append(
                    MarkdownFormatter._format_pipe_row(["---"] * column_count, column_count)
                )
                continue
            repaired.append(MarkdownFormatter._format_pipe_row(rows[index], column_count))
            if index == 0 and not separator_seen and (len(group) == 1 or not MarkdownFormatter._is_separator_row(group[1])):
                repaired.append(
                    MarkdownFormatter._format_pipe_row(["---"] * column_count, column_count)
                )
                separator_seen = True
        return repaired

    @staticmethod
    def _repair_pipe_tables(text: str) -> str:
        repaired: list[str] = []
        group: list[str] = []

        def flush_group() -> None:
            nonlocal group
            if group:
                repaired.extend(MarkdownFormatter._repair_pipe_table_group(group))
                group = []

        for line in text.splitlines():
            if MarkdownFormatter._is_pipe_row(line):
                group.append(line)
                continue
            flush_group()
            repaired.append(line)
        flush_group()
        return "\n".join(repaired)
