<?php

declare(strict_types=1);

require_once dirname(__DIR__) . '/service_ticket_bridge.php';

use MasterMobile\SiteServiceRequests\ServiceTicketBridge;

final class TicketLogFilterFixtureResult
{
    /** @var array<int, array<string, mixed>> */
    private $rows;

    public function __construct(array $rows)
    {
        $this->rows = array_values($rows);
    }

    public function Fetch()
    {
        return array_shift($this->rows) ?: false;
    }
}

final class TicketLogFilterFixtureDatabase
{
    public function Query($sql)
    {
        $sql = (string) $sql;
        $filtersLogs = strpos($sql, "`IS_LOG` = 'N'") !== false;

        if (strpos($sql, 'SELECT `ID` FROM `b_ticket_message`') !== false) {
            return new TicketLogFilterFixtureResult(array(
                array('ID' => $filtersLogs ? 1780 : 1781),
            ));
        }
        if (strpos($sql, 'FROM `b_ticket_message` WHERE') !== false) {
            $rows = array(
                array(
                    'ID' => 1780,
                    'DATE_CREATE' => '2026-08-30 12:00:00',
                    'MESSAGE' => 'Сообщение клиента',
                    'MESSAGE_BY_SUPPORT_TEAM' => 'N',
                    'IS_HIDDEN' => 'N',
                ),
            );
            if (!$filtersLogs) {
                $rows[] = array(
                    'ID' => 1781,
                    'DATE_CREATE' => '2026-08-30 12:00:01',
                    'MESSAGE' => 'Системная запись',
                    'MESSAGE_BY_SUPPORT_TEAM' => 'N',
                    'IS_HIDDEN' => 'N',
                );
            }
            return new TicketLogFilterFixtureResult($rows);
        }
        if (strpos($sql, 'FROM `b_ticket_message_2_file`') !== false) {
            return new TicketLogFilterFixtureResult(array());
        }
        return false;
    }
}

$GLOBALS['DB'] = new TicketLogFilterFixtureDatabase();

$latestMessageId = new ReflectionMethod(ServiceTicketBridge::class, 'latestMessageId');
$latestMessageId->setAccessible(true);
$ticketHistory = new ReflectionMethod(ServiceTicketBridge::class, 'ticketHistory');
$ticketHistory->setAccessible(true);

$history = $ticketHistory->invoke(null, 760, 1781);
$actual = array(
    'latestMessageId' => $latestMessageId->invoke(null, 760),
    'historyMessageIds' => array_column($history, 'messageId'),
    'historyAuthorKinds' => array_column($history, 'authorKind'),
);
$expected = array(
    'latestMessageId' => 1780,
    'historyMessageIds' => array(1780),
    'historyAuthorKinds' => array('customer'),
);

if ($actual !== $expected) {
    fwrite(STDERR, "ticket log filter fixture mismatch\n");
    exit(1);
}

echo json_encode($actual, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . PHP_EOL;
