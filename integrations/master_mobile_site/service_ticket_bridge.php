<?php

declare(strict_types=1);

namespace MasterMobile\SiteServiceRequests {
    final class BridgeFailure extends \RuntimeException
    {
        /** @var string */
        private $errorCode;

        /** @var int|null */
        private $httpStatus;

        public function __construct($errorCode, $httpStatus = null)
        {
            parent::__construct((string) $errorCode);
            $this->errorCode = (string) $errorCode;
            $this->httpStatus = $httpStatus === null ? null : (int) $httpStatus;
        }

        public function errorCode()
        {
            return $this->errorCode;
        }

        public function httpStatus()
        {
            return $this->httpStatus;
        }
    }

    /**
     * Versioned site-side bridge for Bitrix support tickets.
     *
     * Including this file performs no schema, option, Agent, or network writes.
     * registerHandlers(), installSchema(), installSupportUserFields(), and Agent
     * registration are explicit rollout actions.
     */
    final class ServiceTicketBridge
    {
        public const OUTBOX_TABLE = 'b_mm_service_ticket_outbox';
        public const SUPPORT_ENTITY_ID = 'SUPPORT';
        public const PHONE_FIELD = 'UF_MM_SERVICE_PHONE';
        public const ORDER_FIELD = 'UF_MM_SERVICE_ORDER_NUMBER';
        public const REQUEST_TYPE_FIELD = 'UF_MM_SERVICE_REQUEST_TYPE';
        public const COMMAND_MARKER_PREFIX = 'mm-site-service-command:';
        public const AGENT_FUNCTION = 'mm_site_service_ticket_agent();';

        private const OPTION_MODULE = 'main';
        private const OPTION_API_BASE_URL = 'mm_site_service_requests_api_base_url';
        private const OPTION_HMAC_SECRET = 'mm_site_service_requests_hmac_secret';
        private const OPTION_EMIT_ENABLED = 'mm_site_service_requests_emit_enabled';
        private const OPTION_OUTBOUND_ENABLED = 'mm_site_service_requests_outbound_enabled';
        private const OPTION_SUPPORT_USER_ID = 'mm_site_service_requests_support_user_id';
        private const OPTION_BATCH_SIZE = 'mm_site_service_requests_batch_size';
        private const DEFAULT_BATCH_SIZE = 20;
        private const MAX_FILE_BYTES = 10485760;
        private const RETRY_DELAYS = array(60, 120, 300, 900, 1800);
        private const REQUEST_TYPES = array(
            'warranty',
            'refund_money',
            'replacement',
            'delivery_return',
            'consultation',
            'other',
        );

        private function __construct()
        {
        }

        public static function registerHandlers()
        {
            if (!function_exists('AddEventHandler')) {
                throw new BridgeFailure('bitrix_event_api_unavailable');
            }
            AddEventHandler('support', 'OnAfterTicketAdd', array(__CLASS__, 'onAfterTicketAdd'));
            AddEventHandler(
                'support',
                'OnAfterTicketUpdate',
                array(__CLASS__, 'onAfterTicketUpdate')
            );
        }

        public static function onAfterTicketAdd(...$arguments)
        {
            self::captureEvent('OnAfterTicketAdd', $arguments);
        }

        public static function onAfterTicketUpdate(...$arguments)
        {
            self::captureEvent('OnAfterTicketUpdate', $arguments);
        }

        public static function extractEventReference($eventName, array $arguments)
        {
            $ids = array('ticketId' => 0, 'messageId' => 0);
            foreach ($arguments as $argument) {
                self::collectEventIds($argument, $ids);
            }
            if ($ids['ticketId'] <= 0) {
                return null;
            }
            return array(
                'ticketId' => (int) $ids['ticketId'],
                'messageId' => (int) $ids['messageId'],
                'eventType' => $eventName === 'OnAfterTicketAdd'
                    ? 'ticket.created'
                    : 'message.created',
            );
        }

        private static function collectEventIds($value, array &$ids)
        {
            if (is_array($value)) {
                foreach ($value as $key => $nested) {
                    $normalizedKey = strtoupper((string) $key);
                    if (
                        $ids['ticketId'] <= 0
                        && in_array($normalizedKey, array('ID', 'TICKET_ID', 'TICKETID'), true)
                        && is_numeric($nested)
                    ) {
                        $ids['ticketId'] = (int) $nested;
                    }
                    if (
                        $ids['messageId'] <= 0
                        && in_array(
                            $normalizedKey,
                            array('MID', 'MESSAGE_ID', 'MESSAGEID'),
                            true
                        )
                        && is_numeric($nested)
                    ) {
                        $ids['messageId'] = (int) $nested;
                    }
                    if (is_array($nested)) {
                        self::collectEventIds($nested, $ids);
                    }
                }
                return;
            }
            if (!is_numeric($value)) {
                return;
            }
            if ($ids['ticketId'] <= 0) {
                $ids['ticketId'] = (int) $value;
            } elseif ($ids['messageId'] <= 0) {
                $ids['messageId'] = (int) $value;
            }
        }

        private static function captureEvent($eventName, array $arguments)
        {
            if (!self::optionEnabled(self::OPTION_EMIT_ENABLED)) {
                return;
            }
            try {
                $reference = self::extractEventReference($eventName, $arguments);
                if ($reference === null) {
                    throw new BridgeFailure('event_identity_missing');
                }
                if ($reference['messageId'] <= 0) {
                    $reference['messageId'] = self::latestMessageId($reference['ticketId']);
                }
                if ($reference['messageId'] <= 0) {
                    throw new BridgeFailure('event_message_missing');
                }
                self::enqueueEvent(
                    $reference['ticketId'],
                    $reference['messageId'],
                    $reference['eventType']
                );
            } catch (\Throwable $error) {
                self::safeLog('event_capture_failed');
            }
        }

        public static function outboxDdl()
        {
            return "CREATE TABLE IF NOT EXISTS `" . self::OUTBOX_TABLE . "` (\n"
                . "  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
                . "  `event_key` VARCHAR(191) NOT NULL,\n"
                . "  `ticket_id` INT UNSIGNED NOT NULL,\n"
                . "  `message_id` INT UNSIGNED NOT NULL,\n"
                . "  `event_type` VARCHAR(32) NOT NULL,\n"
                . "  `attempts` INT UNSIGNED NOT NULL DEFAULT 0,\n"
                . "  `next_attempt_at` DATETIME NOT NULL,\n"
                . "  `sent_at` DATETIME NULL,\n"
                . "  `last_http_status` SMALLINT UNSIGNED NULL,\n"
                . "  `last_error_code` VARCHAR(64) NULL,\n"
                . "  `created_at` DATETIME NOT NULL,\n"
                . "  `updated_at` DATETIME NOT NULL,\n"
                . "  PRIMARY KEY (`id`),\n"
                . "  UNIQUE KEY `ux_mm_service_ticket_event_key` (`event_key`),\n"
                . "  KEY `ix_mm_service_ticket_delivery` (`sent_at`, `next_attempt_at`, `id`)\n"
                . ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci";
        }

        public static function installationPlan()
        {
            return array(
                'ddl' => self::outboxDdl(),
                'supportUserFields' => self::supportUserFieldDefinitions(),
                'agentFunction' => self::AGENT_FUNCTION,
                'featureFlags' => array(
                    self::OPTION_EMIT_ENABLED,
                    self::OPTION_OUTBOUND_ENABLED,
                ),
            );
        }

        public static function installSchema()
        {
            global $DB;
            self::requireLegacyDatabase($DB);
            $DB->Query(self::outboxDdl());
            $result = $DB->Query("SHOW TABLES LIKE '" . self::OUTBOX_TABLE . "'");
            if (!$result || !$result->Fetch()) {
                throw new BridgeFailure('outbox_schema_readback_failed');
            }
        }

        public static function supportUserFieldDefinitions()
        {
            return array(
                array(
                    'ENTITY_ID' => self::SUPPORT_ENTITY_ID,
                    'FIELD_NAME' => self::PHONE_FIELD,
                    'USER_TYPE_ID' => 'string',
                    'XML_ID' => 'MM_SITE_SERVICE_PHONE',
                    'MANDATORY' => 'Y',
                    'EDIT_FORM_LABEL' => array('ru' => 'Телефон'),
                    'LIST_COLUMN_LABEL' => array('ru' => 'Телефон'),
                    'SETTINGS' => array('SIZE' => 30, 'ROWS' => 1),
                ),
                array(
                    'ENTITY_ID' => self::SUPPORT_ENTITY_ID,
                    'FIELD_NAME' => self::ORDER_FIELD,
                    'USER_TYPE_ID' => 'string',
                    'XML_ID' => 'MM_SITE_SERVICE_ORDER_NUMBER',
                    'MANDATORY' => 'N',
                    'EDIT_FORM_LABEL' => array('ru' => 'Номер заказа'),
                    'LIST_COLUMN_LABEL' => array('ru' => 'Номер заказа'),
                    'SETTINGS' => array('SIZE' => 30, 'ROWS' => 1),
                ),
                array(
                    'ENTITY_ID' => self::SUPPORT_ENTITY_ID,
                    'FIELD_NAME' => self::REQUEST_TYPE_FIELD,
                    'USER_TYPE_ID' => 'enumeration',
                    'XML_ID' => 'MM_SITE_SERVICE_REQUEST_TYPE',
                    'MANDATORY' => 'Y',
                    'EDIT_FORM_LABEL' => array('ru' => 'Тип обращения'),
                    'LIST_COLUMN_LABEL' => array('ru' => 'Тип обращения'),
                    'ENUM' => array(
                        'warranty' => 'Гарантия / качество',
                        'refund_money' => 'Возврат денег',
                        'replacement' => 'Замена',
                        'delivery_return' => 'Доставка / возврат товара',
                        'consultation' => 'Консультация',
                        'other' => 'Другое',
                    ),
                ),
            );
        }

        public static function installSupportUserFields()
        {
            if (!class_exists('CUserTypeEntity') || !class_exists('CUserFieldEnum')) {
                throw new BridgeFailure('support_user_field_api_unavailable');
            }
            foreach (self::supportUserFieldDefinitions() as $definition) {
                $existing = \CUserTypeEntity::GetList(
                    array(),
                    array(
                        'ENTITY_ID' => self::SUPPORT_ENTITY_ID,
                        'FIELD_NAME' => $definition['FIELD_NAME'],
                    )
                )->Fetch();
                if ($existing) {
                    if ((string) $existing['USER_TYPE_ID'] !== $definition['USER_TYPE_ID']) {
                        throw new BridgeFailure('support_user_field_type_mismatch');
                    }
                    continue;
                }
                $enum = isset($definition['ENUM']) ? $definition['ENUM'] : array();
                unset($definition['ENUM']);
                $entity = new \CUserTypeEntity();
                $fieldId = (int) $entity->Add($definition);
                if ($fieldId <= 0) {
                    throw new BridgeFailure('support_user_field_create_failed');
                }
                if ($enum) {
                    $values = array();
                    $sort = 100;
                    foreach ($enum as $xmlId => $title) {
                        $values['n' . $sort] = array(
                            'VALUE' => $title,
                            'XML_ID' => $xmlId,
                            'DEF' => 'N',
                            'SORT' => $sort,
                        );
                        $sort += 100;
                    }
                    $enumApi = new \CUserFieldEnum();
                    if (!$enumApi->SetEnumValues($fieldId, $values)) {
                        throw new BridgeFailure('support_user_field_enum_create_failed');
                    }
                }
            }
            self::readbackSupportUserFields();
        }

        private static function readbackSupportUserFields()
        {
            foreach (self::supportUserFieldDefinitions() as $definition) {
                $field = \CUserTypeEntity::GetList(
                    array(),
                    array(
                        'ENTITY_ID' => self::SUPPORT_ENTITY_ID,
                        'FIELD_NAME' => $definition['FIELD_NAME'],
                    )
                )->Fetch();
                if (
                    !$field
                    || (string) $field['USER_TYPE_ID'] !== $definition['USER_TYPE_ID']
                    || (string) $field['MANDATORY'] !== $definition['MANDATORY']
                ) {
                    throw new BridgeFailure('support_user_field_readback_failed');
                }
                if (isset($definition['ENUM'])) {
                    $actualXmlIds = array();
                    $values = \CUserFieldEnum::GetList(
                        array('SORT' => 'ASC'),
                        array('USER_FIELD_ID' => (int) $field['ID'])
                    );
                    while ($values && ($value = $values->Fetch())) {
                        $actualXmlIds[] = (string) $value['XML_ID'];
                    }
                    foreach (array_keys($definition['ENUM']) as $expectedXmlId) {
                        if (!in_array($expectedXmlId, $actualXmlIds, true)) {
                            throw new BridgeFailure('support_user_field_enum_readback_failed');
                        }
                    }
                }
            }
        }

        public static function runAgent()
        {
            try {
                if (self::optionEnabled(self::OPTION_EMIT_ENABLED)) {
                    self::deliverOutbox();
                }
                if (self::optionEnabled(self::OPTION_OUTBOUND_ENABLED)) {
                    self::deliverCommands();
                }
            } catch (\Throwable $error) {
                self::safeLog('agent_failed');
            }
            return self::AGENT_FUNCTION;
        }

        private static function enqueueEvent($ticketId, $messageId, $eventType)
        {
            global $DB;
            self::requireLegacyDatabase($DB);
            $ticketId = (int) $ticketId;
            $messageId = (int) $messageId;
            if ($ticketId <= 0 || $messageId <= 0) {
                throw new BridgeFailure('event_identity_invalid');
            }
            $eventKey = 'site-support:' . $ticketId . ':' . $messageId;
            $safeEventKey = $DB->ForSql($eventKey, 191);
            $safeEventType = $DB->ForSql((string) $eventType, 32);
            $now = date('Y-m-d H:i:s');
            $DB->Query(
                "INSERT IGNORE INTO `" . self::OUTBOX_TABLE . "` "
                . "(`event_key`, `ticket_id`, `message_id`, `event_type`, `attempts`, "
                . "`next_attempt_at`, `created_at`, `updated_at`) VALUES ("
                . "'{$safeEventKey}', {$ticketId}, {$messageId}, '{$safeEventType}', 0, "
                . "'{$now}', '{$now}', '{$now}')"
            );
        }

        private static function latestMessageId($ticketId)
        {
            global $DB;
            self::requireLegacyDatabase($DB);
            $ticketId = (int) $ticketId;
            $result = $DB->Query(
                "SELECT `ID` FROM `b_ticket_message` WHERE `TICKET_ID` = {$ticketId} "
                . "ORDER BY `ID` DESC LIMIT 1"
            );
            $row = $result ? $result->Fetch() : false;
            return $row ? (int) $row['ID'] : 0;
        }

        private static function deliverOutbox()
        {
            global $DB;
            self::requireLegacyDatabase($DB);
            $batchSize = max(
                1,
                min(100, (int) self::option(self::OPTION_BATCH_SIZE, self::DEFAULT_BATCH_SIZE))
            );
            $result = $DB->Query(
                "SELECT * FROM `" . self::OUTBOX_TABLE . "` "
                . "WHERE `sent_at` IS NULL AND `next_attempt_at` <= NOW() "
                . "ORDER BY `id` ASC LIMIT {$batchSize}"
            );
            while ($result && ($row = $result->Fetch())) {
                try {
                    self::deliverOutboxRow($row);
                    self::markOutboxSent((int) $row['id']);
                } catch (BridgeFailure $error) {
                    self::markOutboxFailed(
                        (int) $row['id'],
                        (int) $row['attempts'],
                        $error->errorCode(),
                        $error->httpStatus()
                    );
                } catch (\Throwable $error) {
                    self::markOutboxFailed(
                        (int) $row['id'],
                        (int) $row['attempts'],
                        'unexpected_delivery_error',
                        null
                    );
                }
            }
        }

        private static function deliverOutboxRow(array $row)
        {
            $payload = self::buildEventPayload(
                (int) $row['ticket_id'],
                (int) $row['message_id'],
                (string) $row['event_key'],
                (string) $row['event_type']
            );
            $body = self::encodeJson($payload);
            $response = self::signedRequest(
                'POST',
                '/api/internal/site-service-requests/events',
                $body,
                array('Content-Type: application/json')
            );
            if ($response['status'] !== 202) {
                throw new BridgeFailure('event_http_' . $response['status'], $response['status']);
            }
            $accepted = self::decodeJson($response['body']);
            if ((string) ($accepted['eventId'] ?? '') !== (string) $row['event_key']) {
                throw new BridgeFailure('event_response_mismatch', 202);
            }
            $missingFileIds = $accepted['missingFileIds'] ?? array();
            if (!is_array($missingFileIds)) {
                throw new BridgeFailure('event_response_invalid', 202);
            }
            foreach ($missingFileIds as $fileId) {
                self::uploadEventFile((string) $row['event_key'], (int) $fileId);
            }
        }

        private static function buildEventPayload($ticketId, $messageId, $eventKey, $eventType)
        {
            global $DB, $USER_FIELD_MANAGER;
            self::requireLegacyDatabase($DB);
            $ticketResult = $DB->Query(
                "SELECT `ID`, `SITE_ID`, `OWNER_USER_ID`, `TITLE`, `CLOSED` "
                . "FROM `b_ticket` WHERE `ID` = " . (int) $ticketId . " LIMIT 1"
            );
            $ticket = $ticketResult ? $ticketResult->Fetch() : false;
            if (!$ticket) {
                throw new BridgeFailure('ticket_not_found');
            }
            $ownerUserId = (int) $ticket['OWNER_USER_ID'];
            if ($ownerUserId <= 0) {
                throw new BridgeFailure('ticket_owner_invalid');
            }
            $userFields = array();
            if (is_object($USER_FIELD_MANAGER)) {
                $userFields = (array) $USER_FIELD_MANAGER->GetUserFields(
                    self::SUPPORT_ENTITY_ID,
                    (int) $ticketId
                );
            }
            $phone = trim((string) self::userFieldValue($userFields, self::PHONE_FIELD));
            if ($phone === '') {
                throw new BridgeFailure('ticket_phone_missing');
            }
            $history = self::ticketHistory((int) $ticketId);
            $occurredAt = null;
            foreach ($history as $message) {
                if ((int) $message['messageId'] === (int) $messageId) {
                    $occurredAt = $message['createdAt'];
                    break;
                }
            }
            if ($occurredAt === null) {
                throw new BridgeFailure('event_message_not_in_history');
            }
            return array(
                'schemaVersion' => 1,
                'eventId' => (string) $eventKey,
                'eventType' => (string) $eventType,
                'occurredAt' => $occurredAt,
                'ticket' => array(
                    'id' => (int) $ticket['ID'],
                    'siteId' => trim((string) $ticket['SITE_ID']) ?: 's1',
                    'ownerUserId' => $ownerUserId,
                    'title' => self::limitText(
                        trim((string) $ticket['TITLE']) ?: 'Сервисное обращение',
                        255
                    ),
                    'phone' => self::limitText($phone, 64),
                    'email' => self::ownerEmail($ownerUserId),
                    'orderNumber' => self::nullableText(
                        self::userFieldValue($userFields, self::ORDER_FIELD),
                        64
                    ),
                    'requestType' => self::requestType(
                        self::userFieldValue($userFields, self::REQUEST_TYPE_FIELD)
                    ),
                    'isClosed' => (string) $ticket['CLOSED'] === 'Y',
                ),
                'history' => $history,
            );
        }

        private static function ticketHistory($ticketId)
        {
            global $DB;
            $messages = array();
            $result = $DB->Query(
                "SELECT `ID`, `DATE_CREATE`, `MESSAGE`, `MESSAGE_BY_SUPPORT_TEAM` "
                . "FROM `b_ticket_message` WHERE `TICKET_ID` = " . (int) $ticketId
                . " ORDER BY `ID` ASC"
            );
            while ($result && ($row = $result->Fetch())) {
                $messages[] = array(
                    'messageId' => (int) $row['ID'],
                    'authorKind' => (string) $row['MESSAGE_BY_SUPPORT_TEAM'] === 'Y'
                        ? 'support'
                        : 'customer',
                    'createdAt' => self::isoDate((string) $row['DATE_CREATE']),
                    'text' => self::limitText(self::plainText((string) $row['MESSAGE']), 200000),
                    'files' => self::messageFiles((int) $row['ID']),
                );
            }
            if (!$messages) {
                throw new BridgeFailure('ticket_history_missing');
            }
            return $messages;
        }

        private static function messageFiles($messageId)
        {
            global $DB;
            $files = array();
            $result = $DB->Query(
                "SELECT F.`ID`, F.`FILE_NAME`, F.`CONTENT_TYPE`, F.`FILE_SIZE` "
                . "FROM `b_ticket_message_2_file` MF "
                . "INNER JOIN `b_file` F ON F.`ID` = MF.`FILE_ID` "
                . "WHERE MF.`MESSAGE_ID` = " . (int) $messageId . " ORDER BY F.`ID` ASC"
            );
            while ($result && ($row = $result->Fetch())) {
                $metadata = self::fileMetadata($row);
                $files[] = array(
                    'fileId' => $metadata['fileId'],
                    'name' => $metadata['name'],
                    'mimeType' => $metadata['mimeType'],
                    'size' => $metadata['size'],
                    'sha256' => $metadata['sha256'],
                );
            }
            return $files;
        }

        private static function uploadEventFile($eventKey, $fileId)
        {
            $metadata = self::loadFileMetadata((int) $fileId);
            $body = file_get_contents($metadata['path']);
            if ($body === false || strlen($body) !== $metadata['size']) {
                throw new BridgeFailure('file_read_failed');
            }
            $path = '/api/internal/site-service-requests/events/'
                . rawurlencode((string) $eventKey)
                . '/files/' . (int) $fileId;
            $asciiName = preg_replace('/[^A-Za-z0-9._-]+/', '_', $metadata['name']);
            if (!is_string($asciiName) || $asciiName === '') {
                $asciiName = 'attachment.bin';
            }
            $disposition = 'Content-Disposition: attachment; filename="'
                . str_replace('"', '', $asciiName)
                . '"; filename*=UTF-8\'\'' . rawurlencode($metadata['name']);
            $response = self::signedRequest(
                'PUT',
                $path,
                $body,
                array(
                    'Content-Type: ' . $metadata['mimeType'],
                    'Content-Length: ' . $metadata['size'],
                    $disposition,
                )
            );
            if ($response['status'] !== 200) {
                throw new BridgeFailure('file_http_' . $response['status'], $response['status']);
            }
        }

        private static function loadFileMetadata($fileId)
        {
            global $DB;
            $result = $DB->Query(
                "SELECT `ID`, `FILE_NAME`, `CONTENT_TYPE`, `FILE_SIZE` FROM `b_file` "
                . "WHERE `ID` = " . (int) $fileId . " LIMIT 1"
            );
            $row = $result ? $result->Fetch() : false;
            if (!$row) {
                throw new BridgeFailure('file_not_found');
            }
            return self::fileMetadata($row);
        }

        private static function fileMetadata(array $row)
        {
            if (!class_exists('CFile')) {
                throw new BridgeFailure('file_api_unavailable');
            }
            $relativePath = (string) \CFile::GetPath((int) $row['ID']);
            $documentRoot = class_exists('Bitrix\\Main\\Application')
                ? (string) \Bitrix\Main\Application::getDocumentRoot()
                : (string) ($_SERVER['DOCUMENT_ROOT'] ?? '');
            $documentRoot = rtrim($documentRoot, '/');
            $path = $documentRoot . '/' . ltrim($relativePath, '/');
            $size = (int) $row['FILE_SIZE'];
            if (
                $size < 0
                || $size > self::MAX_FILE_BYTES
                || !is_file($path)
                || !is_readable($path)
            ) {
                throw new BridgeFailure('file_unavailable');
            }
            $sha256 = hash_file('sha256', $path);
            if (!is_string($sha256) || strlen($sha256) !== 64) {
                throw new BridgeFailure('file_hash_failed');
            }
            $name = basename(str_replace('\\', '/', (string) $row['FILE_NAME']));
            if ($name === '' || $name === '.' || $name === '..') {
                $name = 'attachment-' . (int) $row['ID'] . '.bin';
            }
            return array(
                'fileId' => (int) $row['ID'],
                'name' => self::limitText($name, 255),
                'mimeType' => trim((string) $row['CONTENT_TYPE']) ?: 'application/octet-stream',
                'size' => $size,
                'sha256' => $sha256,
                'path' => $path,
            );
        }

        private static function markOutboxSent($id)
        {
            global $DB;
            $id = (int) $id;
            $DB->Query(
                "UPDATE `" . self::OUTBOX_TABLE . "` SET `sent_at` = NOW(), "
                . "`last_http_status` = 202, `last_error_code` = NULL, `updated_at` = NOW() "
                . "WHERE `id` = {$id} AND `sent_at` IS NULL"
            );
        }

        private static function markOutboxFailed($id, $attempts, $errorCode, $httpStatus)
        {
            global $DB;
            $id = (int) $id;
            $nextAttempts = max(1, (int) $attempts + 1);
            $index = min($nextAttempts, count(self::RETRY_DELAYS)) - 1;
            $delay = $nextAttempts <= count(self::RETRY_DELAYS)
                ? self::RETRY_DELAYS[$index]
                : 3600;
            $nextAttemptAt = date('Y-m-d H:i:s', time() + $delay);
            $safeErrorCode = $DB->ForSql(self::stableCode($errorCode), 64);
            $statusSql = $httpStatus === null ? 'NULL' : (string) max(0, (int) $httpStatus);
            $DB->Query(
                "UPDATE `" . self::OUTBOX_TABLE . "` SET `attempts` = {$nextAttempts}, "
                . "`next_attempt_at` = '{$nextAttemptAt}', `last_http_status` = {$statusSql}, "
                . "`last_error_code` = '{$safeErrorCode}', `updated_at` = NOW() "
                . "WHERE `id` = {$id} AND `sent_at` IS NULL"
            );
        }

        private static function deliverCommands()
        {
            $response = self::signedRequest(
                'GET',
                '/api/internal/site-service-requests/commands',
                '',
                array('Accept: application/json')
            );
            if ($response['status'] !== 200) {
                throw new BridgeFailure('commands_http_' . $response['status'], $response['status']);
            }
            $payload = self::decodeJson($response['body']);
            $commands = $payload['commands'] ?? array();
            if (!is_array($commands)) {
                throw new BridgeFailure('commands_response_invalid', 200);
            }
            foreach (array_slice($commands, 0, 20) as $command) {
                self::deliverCommand(is_array($command) ? $command : array());
            }
        }

        private static function deliverCommand(array $command)
        {
            $commandId = (int) ($command['commandId'] ?? 0);
            $ticketId = (int) ($command['ticketId'] ?? 0);
            if ($commandId <= 0 || $ticketId <= 0) {
                return;
            }
            try {
                $messageId = self::applyCommand(
                    $commandId,
                    $ticketId,
                    (string) ($command['replyText'] ?? '')
                );
                $ack = array(
                    'schemaVersion' => 1,
                    'status' => 'applied',
                    'ticketId' => $ticketId,
                    'messageId' => $messageId,
                    'appliedAt' => date(DATE_ATOM),
                );
            } catch (BridgeFailure $error) {
                $allowed = array(
                    'ticket_not_found',
                    'support_user_invalid',
                    'message_write_failed',
                );
                $errorCode = in_array($error->errorCode(), $allowed, true)
                    ? $error->errorCode()
                    : 'message_write_failed';
                $ack = array(
                    'schemaVersion' => 1,
                    'status' => 'failed',
                    'errorCode' => $errorCode,
                );
            }
            self::ackCommand($commandId, $ack);
        }

        public static function commandMarker($commandId)
        {
            return self::COMMAND_MARKER_PREFIX . (int) $commandId;
        }

        public static function findCommandMarkerInRows($commandId, array $rows)
        {
            $expected = self::commandMarker($commandId);
            foreach ($rows as $row) {
                if (
                    is_array($row)
                    && (string) ($row['EXTERNAL_FIELD_1'] ?? '') === $expected
                    && (int) ($row['ID'] ?? 0) > 0
                ) {
                    return (int) $row['ID'];
                }
            }
            return null;
        }

        private static function applyCommand($commandId, $ticketId, $replyText)
        {
            global $DB;
            self::requireSupportModule();
            self::requireLegacyDatabase($DB);
            $marker = self::commandMarker($commandId);
            $existingMessageId = self::findExistingCommandMessageId($ticketId, $marker);
            if ($existingMessageId > 0) {
                return $existingMessageId;
            }
            $ticketResult = $DB->Query(
                "SELECT `ID` FROM `b_ticket` WHERE `ID` = " . (int) $ticketId . " LIMIT 1"
            );
            if (!$ticketResult || !$ticketResult->Fetch()) {
                throw new BridgeFailure('ticket_not_found');
            }
            $supportUserId = (int) self::option(self::OPTION_SUPPORT_USER_ID, 0);
            self::validateSupportUser($supportUserId);
            $plainText = trim(self::plainText($replyText));
            if ($plainText === '') {
                throw new BridgeFailure('message_write_failed');
            }
            $fields = array(
                'ID' => (int) $ticketId,
                'MESSAGE' => $plainText,
                'MESSAGE_AUTHOR_USER_ID' => $supportUserId,
                'MESSAGE_CREATED_USER_ID' => $supportUserId,
                'MESSAGE_BY_SUPPORT_TEAM' => 'Y',
                'EXTERNAL_ID' => (string) (int) $commandId,
                'EXTERNAL_FIELD_1' => $marker,
                'HIDDEN' => 'N',
            );
            self::callTicketSet($fields);
            $messageId = self::findExistingCommandMessageId($ticketId, $marker);
            if ($messageId <= 0) {
                throw new BridgeFailure('message_write_failed');
            }
            return $messageId;
        }

        private static function callTicketSet(array $fields)
        {
            if (!class_exists('CTicket') || !method_exists('CTicket', 'Set')) {
                throw new BridgeFailure('message_write_failed');
            }
            $method = new \ReflectionMethod('CTicket', 'Set');
            $parameters = $method->getParameters();
            $messageId = 0;
            $checkRights = 'N';
            $sendEmail = 'N';
            $sendEvent = 'N';
            $arguments = array(&$fields);
            $secondName = isset($parameters[1])
                ? strtolower($parameters[1]->getName())
                : '';
            if (strpos($secondName, 'mid') !== false || strpos($secondName, 'message') !== false) {
                $arguments[] = &$messageId;
                $arguments[] = &$checkRights;
                $arguments[] = &$sendEmail;
                $arguments[] = &$sendEvent;
            } else {
                $arguments[] = &$checkRights;
                $arguments[] = &$sendEmail;
                $arguments[] = &$sendEvent;
            }
            $arguments = array_slice($arguments, 0, count($parameters));
            try {
                $result = $method->invokeArgs(null, $arguments);
            } catch (\Throwable $error) {
                throw new BridgeFailure('message_write_failed');
            }
            if ($result === false || $result === null) {
                throw new BridgeFailure('message_write_failed');
            }
        }

        private static function findExistingCommandMessageId($ticketId, $marker)
        {
            global $DB;
            $safeMarker = $DB->ForSql((string) $marker, 255);
            $result = $DB->Query(
                "SELECT `ID` FROM `b_ticket_message` WHERE `TICKET_ID` = "
                . (int) $ticketId . " AND `EXTERNAL_FIELD_1` = '{$safeMarker}' "
                . "ORDER BY `ID` ASC LIMIT 1"
            );
            $row = $result ? $result->Fetch() : false;
            return $row ? (int) $row['ID'] : 0;
        }

        private static function validateSupportUser($userId)
        {
            if ($userId <= 0 || !class_exists('CUser')) {
                throw new BridgeFailure('support_user_invalid');
            }
            $result = \CUser::GetByID((int) $userId);
            $user = $result ? $result->Fetch() : false;
            if (!$user || (string) $user['ACTIVE'] !== 'Y') {
                throw new BridgeFailure('support_user_invalid');
            }
            if (method_exists('CTicket', 'IsSupportTeam') && !\CTicket::IsSupportTeam($userId)) {
                throw new BridgeFailure('support_user_invalid');
            }
        }

        private static function ackCommand($commandId, array $payload)
        {
            $path = '/api/internal/site-service-requests/commands/'
                . (int) $commandId . '/ack';
            $response = self::signedRequest(
                'POST',
                $path,
                self::encodeJson($payload),
                array('Content-Type: application/json')
            );
            if ($response['status'] !== 200) {
                throw new BridgeFailure('command_ack_http_' . $response['status'], $response['status']);
            }
        }

        public static function buildSigningInput(
            $timestamp,
            $nonce,
            $method,
            $path,
            $contentSha256
        ) {
            return implode(
                "\n",
                array(
                    'v1',
                    (string) $timestamp,
                    (string) $nonce,
                    strtoupper((string) $method),
                    (string) $path,
                    (string) $contentSha256,
                )
            );
        }

        public static function sign(
            $secret,
            $timestamp,
            $nonce,
            $method,
            $path,
            $body
        ) {
            $contentSha256 = hash('sha256', (string) $body);
            $input = self::buildSigningInput(
                $timestamp,
                $nonce,
                $method,
                $path,
                $contentSha256
            );
            return array(
                'contentSha256' => $contentSha256,
                'signature' => 'v1=' . hash_hmac('sha256', $input, (string) $secret),
            );
        }

        private static function signedRequest($method, $path, $body, array $headers)
        {
            $baseUrl = rtrim((string) self::option(self::OPTION_API_BASE_URL, ''), '/');
            $secret = (string) self::option(self::OPTION_HMAC_SECRET, '');
            if ($baseUrl === '' || strpos($baseUrl, 'https://') !== 0 || $secret === '') {
                throw new BridgeFailure('bridge_auth_not_configured');
            }
            $timestamp = time();
            $nonce = self::uuidV4();
            $signature = self::sign($secret, $timestamp, $nonce, $method, $path, $body);
            $headers[] = 'X-MM-Site-Timestamp: ' . $timestamp;
            $headers[] = 'X-MM-Site-Nonce: ' . $nonce;
            $headers[] = 'X-MM-Site-Content-SHA256: ' . $signature['contentSha256'];
            $headers[] = 'X-MM-Site-Signature: ' . $signature['signature'];
            return self::httpRequest($method, $baseUrl . $path, $body, $headers);
        }

        private static function httpRequest($method, $url, $body, array $headers)
        {
            if (!function_exists('curl_init')) {
                throw new BridgeFailure('curl_unavailable');
            }
            $curl = curl_init((string) $url);
            curl_setopt($curl, CURLOPT_CUSTOMREQUEST, strtoupper((string) $method));
            curl_setopt($curl, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($curl, CURLOPT_FOLLOWLOCATION, false);
            curl_setopt($curl, CURLOPT_CONNECTTIMEOUT, 5);
            curl_setopt($curl, CURLOPT_TIMEOUT, 30);
            curl_setopt($curl, CURLOPT_SSL_VERIFYPEER, true);
            curl_setopt($curl, CURLOPT_SSL_VERIFYHOST, 2);
            curl_setopt($curl, CURLOPT_HTTPHEADER, $headers);
            if (strtoupper((string) $method) !== 'GET') {
                curl_setopt($curl, CURLOPT_POSTFIELDS, $body);
            }
            $responseBody = curl_exec($curl);
            $status = (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
            if ($responseBody === false) {
                curl_close($curl);
                throw new BridgeFailure('network_error');
            }
            curl_close($curl);
            return array('status' => $status, 'body' => (string) $responseBody);
        }

        private static function requireSupportModule()
        {
            if (!class_exists('CModule') || !\CModule::IncludeModule('support')) {
                throw new BridgeFailure('message_write_failed');
            }
        }

        private static function requireLegacyDatabase($database)
        {
            if (!is_object($database) || !method_exists($database, 'Query')) {
                throw new BridgeFailure('site_database_unavailable');
            }
        }

        private static function ownerEmail($userId)
        {
            if (!class_exists('CUser')) {
                return null;
            }
            $result = \CUser::GetByID((int) $userId);
            $user = $result ? $result->Fetch() : false;
            $email = $user ? trim((string) $user['EMAIL']) : '';
            return $email === '' ? null : self::limitText($email, 320);
        }

        private static function userFieldValue(array $fields, $fieldName)
        {
            $field = $fields[(string) $fieldName] ?? null;
            if (!is_array($field)) {
                return null;
            }
            $value = $field['VALUE'] ?? null;
            if (is_array($value)) {
                return reset($value);
            }
            return $value;
        }

        private static function requestType($value)
        {
            $normalized = trim((string) $value);
            if (in_array($normalized, self::REQUEST_TYPES, true)) {
                return $normalized;
            }
            if (ctype_digit($normalized) && class_exists('CUserFieldEnum')) {
                $result = \CUserFieldEnum::GetList(array(), array('ID' => (int) $normalized));
                $enum = $result ? $result->Fetch() : false;
                $xmlId = $enum ? trim((string) $enum['XML_ID']) : '';
                if (in_array($xmlId, self::REQUEST_TYPES, true)) {
                    return $xmlId;
                }
            }
            return 'other';
        }

        private static function isoDate($value)
        {
            try {
                $date = new \DateTimeImmutable((string) $value);
            } catch (\Throwable $error) {
                throw new BridgeFailure('message_date_invalid');
            }
            return $date->format(DATE_ATOM);
        }

        private static function plainText($value)
        {
            $text = preg_replace('/<br\s*\/?\s*>/i', "\n", (string) $value);
            $text = strip_tags((string) $text);
            $text = html_entity_decode($text, ENT_QUOTES | ENT_HTML5, 'UTF-8');
            return str_replace(array("\r\n", "\r"), "\n", $text);
        }

        private static function nullableText($value, $limit)
        {
            $text = trim((string) $value);
            return $text === '' ? null : self::limitText($text, (int) $limit);
        }

        private static function limitText($value, $limit)
        {
            $text = (string) $value;
            if (function_exists('mb_substr')) {
                return mb_substr($text, 0, (int) $limit, 'UTF-8');
            }
            return substr($text, 0, (int) $limit);
        }

        private static function encodeJson(array $payload)
        {
            $json = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
            if (!is_string($json)) {
                throw new BridgeFailure('json_encode_failed');
            }
            return $json;
        }

        private static function decodeJson($value)
        {
            $payload = json_decode((string) $value, true);
            if (!is_array($payload)) {
                throw new BridgeFailure('json_decode_failed');
            }
            return $payload;
        }

        private static function uuidV4()
        {
            $bytes = random_bytes(16);
            $bytes[6] = chr((ord($bytes[6]) & 0x0f) | 0x40);
            $bytes[8] = chr((ord($bytes[8]) & 0x3f) | 0x80);
            $hex = bin2hex($bytes);
            return substr($hex, 0, 8) . '-' . substr($hex, 8, 4) . '-'
                . substr($hex, 12, 4) . '-' . substr($hex, 16, 4) . '-'
                . substr($hex, 20, 12);
        }

        private static function optionEnabled($name)
        {
            return strtoupper((string) self::option($name, 'N')) === 'Y';
        }

        private static function option($name, $default)
        {
            if (!class_exists('Bitrix\\Main\\Config\\Option')) {
                return $default;
            }
            return \Bitrix\Main\Config\Option::get(
                self::OPTION_MODULE,
                (string) $name,
                (string) $default
            );
        }

        private static function stableCode($value)
        {
            $normalized = preg_replace('/[^a-z0-9_]+/', '_', strtolower((string) $value));
            return trim((string) $normalized, '_') ?: 'unknown_error';
        }

        private static function safeLog($code)
        {
            error_log('[mm-site-service-requests] ' . self::stableCode($code));
        }
    }
}

namespace {
    if (!function_exists('mm_site_service_ticket_agent')) {
        function mm_site_service_ticket_agent()
        {
            return \MasterMobile\SiteServiceRequests\ServiceTicketBridge::runAgent();
        }
    }
}
