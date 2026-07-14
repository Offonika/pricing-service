import type { Meta, StoryObj } from "@storybook/react-vite";
import { Button, EmptyState, ErrorState, LoadingState, MetricCard, PageShell, StatusBadge, Surface } from ".";

const meta = { title: "Foundation/Primitives", component: PageShell, parameters: { a11y: { test: "todo" } } } satisfies Meta<typeof PageShell>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Overview: Story = { render: () => <PageShell eyebrow="Pricing UI" title="Состояния управленческой витрины" description="Компактные компоненты для встраиваемых приложений Bitrix24.">
  <Surface style={{ display: "flex", gap: 12, padding: 16, flexWrap: "wrap" }}><Button>Обновить</Button><Button variant="secondary">Назад</Button><StatusBadge tone="success">Готово</StatusBadge><StatusBadge tone="warning">Устарело</StatusBadge></Surface>
  <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
    <MetricCard label="Решений в фокусе" value="8" hint="за сегодня" />
    <MetricCard delta={{ text: "+12,5% к прошлому месяцу", direction: "up", isFavorable: true }} label="Выручка" tone="info" value="1 000 000 ₽" />
    <MetricCard delta={{ text: "+3 п.п.", direction: "up", isFavorable: false }} label="Открытые вопросы" tone="warning" tooltip="Расходы без закрывающих документов." value="4" />
  </div>
  <EmptyState title="Нет решений" /><LoadingState title="Загружаем данные" /><ErrorState title="Источник недоступен" />
</PageShell> };
