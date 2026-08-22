<?php

declare(strict_types=1);

if (!defined('B_PROLOG_INCLUDED') || B_PROLOG_INCLUDED !== true) {
    die();
}

require_once __DIR__ . '/lib/client.php';

final class MasterMobileCustomerSettlementsComponent extends CBitrixComponent
{
    public function executeComponent()
    {
        global $USER;
        $this->disableResponseCaching();
        if (!is_object($USER) || !$USER->IsAuthorized()) {
            $this->arResult = array(
                'status' => 'pilot_disabled',
                'is_stale' => false,
            );
            $this->includeComponentTemplate();
            return;
        }

        $mockVariant = null;
        $host = (string)($_SERVER['HTTP_HOST'] ?? '');
        if (\MasterMobile\CustomerSettlements\Client::mockQueryEnabledForHost($host)) {
            $candidate = (string)($_GET['settlements_state'] ?? '');
            if (in_array($candidate, array(
                'debt', 'advance', 'zero', 'stale', 'unavailable',
                'not_linked', 'ambiguous', 'disabled',
            ), true)) {
                $mockVariant = $candidate;
            }
        }
        $result = \MasterMobile\CustomerSettlements\Client::summaryForUser(
            (string)$USER->GetID(),
            false,
            $mockVariant
        );
        unset($result['_transport_error'], $result['_error_code']);
        $this->arResult = $result;
        $this->includeComponentTemplate();
    }

    private function disableResponseCaching(): void
    {
        if (!headers_sent()) {
            header('Cache-Control: private, no-store, no-cache, must-revalidate');
            header('Pragma: no-cache');
            header('Expires: 0');
        }
        if (class_exists('\\Bitrix\\Main\\Context')) {
            $response = \Bitrix\Main\Context::getCurrent()->getResponse();
            $response->addHeader('Cache-Control', 'private, no-store, no-cache, must-revalidate');
            $response->addHeader('Pragma', 'no-cache');
        }
    }
}
