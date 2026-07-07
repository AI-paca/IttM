from app.chunking.aligned_rows import aligned_numeric_text_to_markdown


def test_aligned_numeric_text_builds_ranked_markdown_table():
    result = aligned_numeric_text_to_markdown(
        "\n".join(
            [
                "Global Top 3 devices",
                "Data Source: benchmark",
                "1 Alpha Phone 1863133",
                "2 Beta Phone 1532816",
                "8 Gamma Phone 1466153",
            ]
        )
    )

    assert result is not None
    assert result.rows == 3
    assert result.cols == 3
    assert "| Rank | Item | Score |" in result.markdown
    assert "| 2 | Beta Phone | 1532816 |" in result.markdown
    assert "| 3 | Gamma Phone | 1466153 |" in result.markdown


def test_aligned_numeric_text_splits_phone_benchmark_rows():
    result = aligned_numeric_text_to_markdown(
        "\n".join(
            [
                "Global Top 7 Best Performing Sub-flagship Phones, November 2025",
                "Data Source: Antutu Benchmark V11 М Е.",
                "1 PocoX7 Pro Dimensity8400-Ultra 12GB+512GB 1863133",
                "2 PocoX6Pro5G Dimensity8300-Ultra 12GB+512GB 1532816",
                "4 InfinixGT20Pro_Dimensity8200 Ultimate 12GB+256GB 1301823",
                "5 OnePlusNord4 snapdragon7+Gen3 8GB+256GB 1276239",
                "6 РосоЕБ Snapdragon7+Gen2 12GB+256GB 1252520",
                "7 1000 29 snapdragon7Gen3 8GB+256GB 1011022",
                "9 Motorola Edge 60 Fusion Dimensity7300-Utra 868+25668 881908",
            ]
        )
    )

    assert result is not None
    assert result.rows == 7
    assert result.cols == 5
    assert "*average score" in result.markdown
    assert "М Е." not in result.markdown
    assert "| Rank | Model | Chipset | Memory | Score |" in result.markdown
    assert "| 1 | Poco X7 Pro | Dimensity 8400-Ultra | 12GB+512GB | 1863133 |" in result.markdown
    assert "| 3 | Infinix GT 20 Pro | Dimensity 8200 Ultimate | 12GB+256GB | 1301823 |" in result.markdown
    assert "| 4 | OnePlus Nord 4 | Snapdragon 7+ Gen 3 | 8GB+256GB | 1276239 |" in result.markdown
    assert "| 5 | Poco F5 | Snapdragon 7+ Gen 2 | 12GB+256GB | 1252520 |" in result.markdown
    assert "| 6 | iQOO Z9 | Snapdragon 7 Gen 3 | 8GB+256GB | 1011022 |" in result.markdown
    assert "| 7 | Motorola Edge 60 Fusion | Dimensity 7300-Ultra | 8GB+256GB | 881908 |" in result.markdown


def test_aligned_numeric_text_builds_stacked_name_value_table():
    result = aligned_numeric_text_to_markdown(
        "\n".join(
            [
                "Кавтаев",
                "Кононюк,",
                "Кудрявцева",
                "Чалурин",
                "6",
                "7",
                "5",
            ]
        )
    )

    assert result is not None
    assert result.rows == 4
    assert result.cols == 2
    assert "| Фамилия | Балл |" in result.markdown
    assert "| Кавтаев | - |" in result.markdown
    assert "| Кононюк | 6 |" in result.markdown
    assert "| Чапурин | 5 |" in result.markdown


def test_aligned_numeric_text_builds_two_column_value_table():
    result = aligned_numeric_text_to_markdown("Alpha 7\nBeta 12\nGamma 3")

    assert result is not None
    assert result.cols == 2
    assert "| Item | Value |" in result.markdown
    assert "| Gamma | 3 |" in result.markdown


def test_aligned_numeric_text_keeps_unstructured_text_plain():
    assert aligned_numeric_text_to_markdown("Report 2025\nPage 1") is None
