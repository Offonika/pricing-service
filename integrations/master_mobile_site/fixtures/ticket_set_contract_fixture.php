<?php

declare(strict_types=1);

namespace {
    final class CTicket
    {
        public static array $call = array();

        public static function Set(
            $arFields,
            &$MID,
            $id = "",
            $checkRights = "Y",
            $sendEmailToAuthor = "Y",
            $sendEmailToTechsupport = "Y"
        ) {
            self::$call = array(
                'ticketId' => (int) $id,
                'checkRights' => (string) $checkRights,
                'sendEmailToAuthor' => (string) $sendEmailToAuthor,
                'sendEmailToTechsupport' => (string) $sendEmailToTechsupport,
                'fieldTicketId' => (int) ($arFields['ID'] ?? 0),
            );
            $MID = 8123;
            return (int) $id;
        }
    }
}

namespace {
    require_once dirname(__DIR__) . '/service_ticket_bridge.php';

    $method = new ReflectionMethod(
        MasterMobile\SiteServiceRequests\ServiceTicketBridge::class,
        'callTicketSet'
    );
    $method->setAccessible(true);
    $method->invokeArgs(null, array(array('ID' => 741, 'MESSAGE' => 'reply'), 741));

    $expected = array(
        'ticketId' => 741,
        'checkRights' => 'N',
        'sendEmailToAuthor' => 'N',
        'sendEmailToTechsupport' => 'N',
        'fieldTicketId' => 741,
    );
    if (CTicket::$call !== $expected) {
        fwrite(STDERR, "CTicket::Set contract fixture mismatch\n");
        exit(1);
    }

    echo json_encode(CTicket::$call, JSON_UNESCAPED_SLASHES) . PHP_EOL;
}
