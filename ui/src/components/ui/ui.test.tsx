import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button, ErrorState, StatusBadge } from ".";

describe("pricing UI primitives", () => {
  it("renders accessible actions and states", () => {
    render(<><Button>Обновить</Button><StatusBadge tone="warning">Устарело</StatusBadge><ErrorState title="Источник недоступен" /></>);
    expect(screen.getByRole("button", { name: "Обновить" })).toBeEnabled();
    expect(screen.getByText("Устарело")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Источник недоступен");
  });
});
