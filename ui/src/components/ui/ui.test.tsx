import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button, ErrorState, MetricCard, StatusBadge } from ".";

describe("pricing UI primitives", () => {
  it("renders accessible actions and states", () => {
    render(<><Button>Обновить</Button><StatusBadge tone="warning">Устарело</StatusBadge><ErrorState title="Источник недоступен" /></>);
    expect(screen.getByRole("button", { name: "Обновить" })).toBeEnabled();
    expect(screen.getByText("Устарело")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Источник недоступен");
  });

  it("renders a metric card with tone, delta and a hover/focus tooltip", () => {
    render(
      <MetricCard
        delta={{ text: "+12,5% к прошлому месяцу", direction: "up", isFavorable: true }}
        label="Выручка"
        tone="success"
        tooltip="Сумма продаж с начала месяца."
        value="1 000 000 ₽"
      />
    );
    expect(screen.getByText("1 000 000 ₽")).toBeVisible();
    expect(screen.getByText(/\+12,5% к прошлому месяцу/)).toBeVisible();
    expect(screen.queryByRole("tooltip")).toBeNull();

    const trigger = screen.getByRole("button", { name: "Пояснение: Выручка" });
    fireEvent.mouseEnter(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Сумма продаж с начала месяца.");
    fireEvent.mouseLeave(trigger);
    expect(screen.queryByRole("tooltip")).toBeNull();

    fireEvent.focus(trigger);
    expect(screen.getByRole("tooltip")).toBeVisible();
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
