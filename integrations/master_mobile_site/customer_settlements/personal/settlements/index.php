<?php

define('BX_COMPOSITE_CACHE', false);
require($_SERVER['DOCUMENT_ROOT'] . '/bitrix/header.php');
$APPLICATION->SetTitle('Взаиморасчёты');
?>

<div class="profile">
    <div class="container container--big">
        <div class="profile__row row">
            <div class="profile__sidebar">
                <nav class="profile__menu">
                    <?php include($_SERVER['DOCUMENT_ROOT'] . '/local/include/personal/personal_menu.php'); ?>
                </nav>
                <div class="profile__card-type profile__card-type--desktop card-type">
                    <?php include($_SERVER['DOCUMENT_ROOT'] . '/local/include/personal/card.php'); ?>
                </div>
            </div>
            <div class="profile__content">
                <?php $APPLICATION->IncludeComponent(
                    'mastermobile:customer.settlements',
                    '',
                    array('CACHE_TYPE' => 'N'),
                    false,
                    array('HIDE_ICONS' => 'Y')
                ); ?>
                <div class="profile__card-type profile__card-type--mob card-type">
                    <?php include($_SERVER['DOCUMENT_ROOT'] . '/local/include/personal/card.php'); ?>
                </div>
            </div>
        </div>
    </div>
</div>

<?php require($_SERVER['DOCUMENT_ROOT'] . '/bitrix/footer.php'); ?>
