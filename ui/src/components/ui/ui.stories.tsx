import type { Meta, StoryObj } from "@storybook/react-vite";
import { Button, EmptyState, ErrorState, LoadingState, MetricCard, PageShell, StatusBadge, Surface } from ".";

const meta = { title: "Foundation/Primitives", component: PageShell, parameters: { a11y: { test: "todo" } } } satisfies Meta<typeof PageShell>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Overview: Story = { render: () => <PageShell eyebrow="Pricing UI" title="Состояния управленческой витрины" description="Компактные компоненты для встраиваемых приложений Bitrix24.">
  <Surface style={{ display: "flex", gap: 12, padding: 16, flexWrap: "wrap" }}><Button>Обновить</Button><Button variant="secondary">Назад</Button><StatusBadge tone="success">Готово</StatusBadge><StatusBadge tone="warning">Устарело</StatusBadge></Surface>
  <MetricCard label="Решений в фокусе" value="8" hint="за сегодня" /><EmptyState title="Нет решений" /><LoadingState title="Загружаем данные" /><ErrorState title="Источник недоступен" />
</PageShell> };
