from app.services.finance_cash_position import classify_money_place


def test_classify_money_place_separates_savings_cards_and_cashboxes() -> None:
    assert (
        classify_money_place(
            "Сберегательный счет 3277",
            ["Сберегательный счет 3277", "Cберсчета"],
        )
        == "savings_accounts"
    )
    assert (
        classify_money_place(
            "5462 карта Гейдаров",
            ["5462 карта Гейдаров", "Сайт", "Банковские счета"],
        )
        == "savings_accounts"
    )
    assert (
        classify_money_place(
            "3015 карта Т-Банк Аннамурадов Влад",
            ["3015 карта Т-Банк Аннамурадов Влад", "Kарты"],
        )
        == "cards"
    )
    assert (
        classify_money_place(
            "СПБ Садовая касса 1",
            ["СПБ Садовая касса 1", "СПБ Садовая"],
        )
        == "cashboxes"
    )
