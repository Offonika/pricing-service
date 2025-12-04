# TopControl DB schema
Generated 2025-11-23T18:34:31Z from 77.39.29.183:1433 db TopControl.
Total tables: 201

## dbo.currency
- id | int | not null | PK
- naim_long | varchar(15) | null | default (NULL)
- naim_short | varchar(5) | not null
- sys_num | tinyint | null | default (NULL)

## dbo.desktop_inform_sett
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- tip_setting | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- id_infostena_desktop | int | not null

## dbo.doc_response
- id | int | not null | PK
- naim | varchar(100) | not null
- kod_infsys | varchar(70) | not null

## dbo.doc_zakup
- id | int | not null | PK
- kod_infsys | varchar(70) | not null
- naim | varchar(100) | not null

## dbo.ei
- id | int | not null | PK
- naim | varchar(100) | not null
- sys_num | int | null | default (NULL)
- kod_infsys | varchar(70) | null | default (NULL)

## dbo.ei_klass
- id | int | not null | PK
- sys_num | int | null | default (NULL)
- naim_long_ei | varchar(80) | not null
- naim_short_ei | varchar(80) | not null
- kod_infsys | varchar(70) | null | default (NULL)

## dbo.ei_klass_ei_tovar
- id | int | not null | PK
- id_ei | int | not null
- id_ei_klass | int | not null

## dbo.ei_rules
- id | int | not null | PK
- naim | varchar(50) | not null
- rulefortovar | tinyint | not null | default ((0))
- ei_from | int | null | default (NULL)
- ei_to | int | null | default (NULL)
- field1 | int | null | default (NULL)
- field2 | int | null | default (NULL)
- val1 | float | null | default (NULL)
- val2 | float | null | default (NULL)
- koff1 | tinyint | null | default ((0))
- koff2 | tinyint | null | default ((0))
- operat | varchar(1) | null | default (NULL)
- val3 | float | null | default (NULL)
- koff3 | tinyint | null | default ((0))
- field3 | int | null | default (NULL)
- operat2 | varchar(1) | null | default (NULL)
- skobka1 | varchar(1) | null | default (NULL)
- skobka2 | varchar(1) | null | default (NULL)
- id_ei_klass | int | null | default (NULL)
- id_ei_klass_1 | int | null | default (NULL)

## dbo.ei_tovar
- id | int | not null | PK
- koff | float | null | default (NULL)
- auto_gen | tinyint | null | default ((0))
- id_ei | int | null | default (NULL)
- id_ei_klass | int | null | default (NULL)
- id_tovar | int | not null

## dbo.elem_166
- id | int | not null | PK
- fprop314 | varchar(100) | not null

## dbo.elem_167
- id | int | not null | PK
- fprop315 | varchar(100) | not null

## dbo.elem_168
- id | int | not null | PK
- fprop316 | varchar(100) | not null

## dbo.elem_169
- id | int | not null | PK
- fprop317 | varchar(100) | not null

## dbo.elem_17
- id | int | not null | PK
- fprop31 | varchar(100) | not null

## dbo.elem_171
- id | int | not null | PK
- fprop324 | varchar(60) | not null

## dbo.elem_172
- id | int | not null | PK
- fprop325 | varchar(100) | not null

## dbo.elem_173
- id | int | not null | PK
- fprop326 | varchar(100) | not null

## dbo.elem_174
- id | int | not null | PK
- fprop327 | varchar(100) | not null

## dbo.elem_19
- id | int | not null | PK
- fprop32 | varchar(100) | not null

## dbo.elem_20
- id | int | not null | PK
- fprop34 | varchar(100) | not null

## dbo.elem_209
- id | int | not null | PK
- fprop403 | varchar(100) | not null

## dbo.elem_21
- id | int | not null | PK
- fprop35 | varchar(100) | not null

## dbo.elem_22
- id | int | not null | PK
- fprop36 | varchar(100) | not null

## dbo.elem_23
- id | int | not null | PK
- fprop38 | varchar(40) | not null

## dbo.elem_24
- id | int | not null | PK
- fprop43 | varchar(60) | not null

## dbo.elem_25
- id | int | not null | PK
- fprop44 | varchar(60) | not null

## dbo.elem_26
- id | int | not null | PK
- fprop45 | varchar(100) | not null

## dbo.elem_28
- id | int | not null | PK
- fprop47 | varchar(100) | not null

## dbo.elem_29
- id | int | not null | PK
- fprop48 | varchar(100) | not null

## dbo.elem_30
- id | int | not null | PK
- fprop49 | varchar(100) | not null

## dbo.elem_31
- id | int | not null | PK
- fprop50 | varchar(100) | not null

## dbo.elem_32
- id | int | not null | PK
- fprop39 | varchar(40) | not null

## dbo.elem_33
- id | int | not null | PK
- fprop42 | varchar(40) | not null

## dbo.elem_34
- id | int | not null | PK
- fprop41 | varchar(100) | null | default (NULL)
- fprop321 | varchar(40) | not null

## dbo.elem_35
- id | int | not null | PK
- fprop37 | varchar(100) | not null

## dbo.elem_49
- id | int | not null | PK
- fprop67 | varchar(50) | not null

## dbo.holding
- id | int | not null | PK
- kod_infsys | varchar(50) | not null
- naim | varchar(200) | null | default (NULL)

## dbo.holidays
- id | int | not null | PK
- naim | varchar(50) | not null
- dt | datetime | not null

## dbo.infostena_desktop
- id | int | not null | PK
- ord_num | int | null | default (NULL)
- id_owner | int | not null
- naim | varchar(60) | not null
- auto_count | tinyint | not null | default ((0))

## dbo.infostena_settings
- id | int | not null | PK
- id_owner | int | not null
- tip_setting | int | not null
- int_val | int | not null

## dbo.infostena_to_desktop
- id | int | not null | PK
- x_screen | int | not null
- y_screen | int | not null
- is_collapsed | tinyint | not null | default ((0))
- h_obj | smallint | null | default (NULL)
- w_obj | smallint | null | default (NULL)
- size_obj | tinyint | null | default (NULL)
- id_obj | int | not null
- tip_obj | int | not null
- id_infostena_desktop | int | not null

## dbo.infsys
- id | int | not null | PK
- naim | varchar(50) | not null

## dbo.klient
- id | int | not null | PK
- naim | varchar(200) | not null
- kod_infsys | varchar(100) | not null
- iskluchrasch | tinyint | null | default ((0))
- sobst_filial | tinyint | null | default ((0))
- kod_infsys1 | varchar(100) | null | default (NULL)
- fprop323 | varchar(250) | null | default (NULL)
- fprop322 | varchar(250) | null | default (NULL)
- id_kontrag | int | null | default (NULL)
- felem34 | int | null | default (NULL)
- felem26 | int | null | default (NULL)
- felem24 | int | null | default (NULL)
- felem32 | int | null | default (NULL)
- felem25 | int | null | default (NULL)
- felem171 | int | null | default (NULL)
- felem33 | int | null | default (NULL)
- felem31 | int | null | default (NULL)
- felem28 | int | null | default (NULL)
- felem29 | int | null | default (NULL)
- felem30 | int | null | default (NULL)
- felem172 | int | null | default (NULL)
- felem173 | int | null | default (NULL)
- felem174 | int | null | default (NULL)
- id_infsys | int | null | default (NULL)
- id_holding | int | null | default (NULL)

## dbo.kontrag
- id | int | not null | PK
- kod_infsys | varchar(100) | not null
- naim | varchar(200) | not null
- iskluchrasch | tinyint | null | default ((0))
- kod_infsys1 | varchar(100) | null | default (NULL)
- naim_long | varchar(200) | null | default (NULL)
- sobst_filial | tinyint | null | default ((0))
- id_infsys | int | null | default (NULL)

## dbo.kontrag_dogovor
- id | int | not null | PK
- otsr_days | int | null | default (NULL)
- dolg_lim | decimal(20,4) | null | default (NULL)
- dogovornum | varchar(40) | not null
- dogovornaim | varchar(300) | not null
- id_kontrag | int | not null

## dbo.matr_ass
- id | int | not null | PK
- elem_value | int | not null
- parent_value | int | null | default (NULL)
- id_elemelem | int | null | default (NULL)
- id_matr_tree | int | not null

## dbo.matr_ass_val
- id | int | not null | PK
- assign_val | int | not null
- id_matr_ass | int | not null

## dbo.matr_iskluch
- id | int | not null | PK
- id_matr_sostav_val | int | not null
- id_matr_ass_val | int | not null

## dbo.matr_plan
- id | int | not null | PK
- pokazatel | int | not null
- plan_val | float | not null
- id_matr_sostav_val | int | null | default (NULL)
- id_matr_ass_val | int | null | default (NULL)

## dbo.matr_plan_itog
- id | int | not null | PK
- pokazatel | int | not null
- plan_val | float | not null
- id_matr_tree | int | not null

## dbo.matr_privileges
- id | int | not null | PK
- is_view | tinyint | not null | default ((0))
- is_edit | tinyint | not null | default ((0))
- is_delete | tinyint | not null | default ((0))
- id_user | int | null | default (NULL)
- id_group | int | null | default (NULL)
- id_matr_tree | int | not null

## dbo.matr_sostav
- id | int | not null | PK
- elem_value | int | not null
- parent_value | int | null | default (NULL)
- id_elemelem | int | null | default (NULL)
- id_matr_tree | int | not null

## dbo.matr_sostav_val
- id | int | not null | PK
- sostav_val | int | not null
- id_matr_sostav | int | not null

## dbo.matr_tree
- id | int | not null | PK
- naim | varchar(150) | not null
- ord_num | int | null | default (NULL)
- ismatriza | tinyint | not null | default ((0))
- id_owner | int | null | default (NULL)
- id_matr_tree | int | null | default (NULL)

## dbo.matr_tree_all
- id | int | not null | PK
- naim | varchar(150) | null | default (NULL)
- ord_num | int | not null
- id_owner | int | null | default (NULL)
- id_matr_tree | int | null | default (NULL)
- id_matr_tree_all | int | null | default (NULL)

## dbo.motivat_list
- id | int | not null | PK
- id_owner | int | not null
- naim | varchar(150) | not null

## dbo.motivat_tree
- id | int | not null | PK
- id_owner | int | not null
- naim | varchar(150) | null | default (NULL)
- ord_num | int | not null
- id_motivat_tree | int | null | default (NULL)
- id_motivat_list | int | null | default (NULL)

## dbo.nelikvid_prizn
- id | int | not null | PK
- kod | tinyint | not null
- naim | varchar(20) | not null

## dbo.ostatki
- id | int | not null | PK
- kol | float | not null
- koff | float | null | default (NULL)
- dt_month | tinyint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- dt_year | smallint | null | default (NULL)
- docdate_ost | datetime | not null
- cena_last | decimal(20,4) | null | default (NULL)
- sum_ost | decimal(20,4) | null | default (NULL)
- cena | decimal(20,4) | null | default (NULL)
- kol_reserv | float | null | default (NULL)
- kol_prixod | float | null | default (NULL)
- kol_rasxod | float | null | default (NULL)
- sum_prixod | decimal(20,4) | null | default (NULL)
- sum_rasxod | decimal(20,4) | null | default (NULL)
- cena2 | decimal(20,4) | null | default (NULL)
- date_parttov | datetime | null | default (NULL)
- num_parttov | varchar(50) | null | default (NULL)
- docdate_nxt | datetime | null | default (NULL)
- kol_prixod2 | float | null | default (NULL)
- sum_prixod2 | decimal(20,4) | null | default (NULL)
- id_ei | int | not null
- id_doc_zakup | int | null | default (NULL)
- id_postavsh | int | null | default (NULL)
- id_sklad | int | not null
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_infsys | int | not null
- id_specificat | int | null | default (NULL)
- id_urlico | int | null | default (NULL)

## dbo.ostatki_reserv
- id | int | not null | PK
- kol | float | not null
- dt_month | tinyint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- dt_year | smallint | null | default (NULL)
- docdate_ost | datetime | not null
- kol1 | float | null | default (NULL)
- id_ei | int | null | default (NULL)
- id_postavsh | int | null | default (NULL)
- id_sklad | int | null | default (NULL)
- id_tovar | int | null | default (NULL)
- id_tovar_specific | int | null | default (NULL)
- id_infsys | int | null | default (NULL)
- id_specificat | int | null | default (NULL)
- id_urlico | int | null | default (NULL)

## dbo.ostatki_start
- id | int | not null | PK
- kol | float | not null
- cena | decimal(20,4) | not null
- koff | float | null | default (NULL)
- dt_month | tinyint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- dt_year | smallint | null | default (NULL)
- docdate_ost | datetime | not null
- kol_reserv | float | null | default (NULL)
- date_parttov | datetime | null | default (NULL)
- num_parttov | varchar(50) | null | default (NULL)
- cena2 | decimal(20,4) | null | default (NULL)
- docdate_nxt | datetime | null | default (NULL)
- id_ei | int | not null
- id_doc_zakup | int | null | default (NULL)
- id_postavsh | int | null | default (NULL)
- id_sklad | int | not null
- id_prodagi_tip | int | null | default (NULL)
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_infsys | int | not null
- id_specificat | int | null | default (NULL)
- id_urlico | int | null | default (NULL)

## dbo.pererasp_sett
- id | int | not null | PK
- correct_perc | float | null | default (NULL)
- srok_days | float | null | default (NULL)
- sklad_prioritet | tinyint | null | default (NULL)
- zakaz_days | smallint | null | default (NULL)
- is_priemka | tinyint | not null | default ((0))
- is_otgryzka | tinyint | not null | default ((0))
- novinka | smallint | null | default (NULL)
- id_zakup_rep_list | int | not null
- id_sklad | int | not null

## dbo.plan_autosumma
- id | int | not null | PK
- lvl1 | int | null | default (NULL)
- lvl2 | int | null | default (NULL)
- lvl3 | int | null | default (NULL)
- lvl4 | int | null | default (NULL)
- lvl5 | int | null | default (NULL)
- lvl6 | int | null | default (NULL)
- lvl7 | int | null | default (NULL)
- lvl8 | int | null | default (NULL)
- lvl9 | int | null | default (NULL)
- lvl10 | int | null | default (NULL)
- id_plan_list | int | not null

## dbo.plan_baseperiod
- id | int | not null | PK
- pokazatel | int | not null
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- with_prognoz | tinyint | null | default ((0))
- id_plan_list | int | not null

## dbo.plan_dop_col
- id | int | not null | PK
- id_elemelem | int | null | default (NULL)
- id_elemprop | int | not null
- id_elemelem1 | int | null | default (NULL)
- id_elemelem2 | int | null | default (NULL)
- id_link_dop | int | null | default (NULL)
- id_link_elel_dop | int | null | default (NULL)
- id_plan_list | int | not null

## dbo.plan_fact_plus_iskl
- id | int | not null | PK
- lvl1 | int | null | default (NULL)
- lvl2 | int | null | default (NULL)
- lvl3 | int | null | default (NULL)
- lvl4 | int | null | default (NULL)
- lvl5 | int | null | default (NULL)
- lvl6 | int | null | default (NULL)
- lvl7 | int | null | default (NULL)
- lvl8 | int | null | default (NULL)
- lvl9 | int | null | default (NULL)
- lvl10 | int | null | default (NULL)
- id_plan_list | int | not null

## dbo.plan_iskl
- id | int | not null | PK
- lvl1 | int | null | default (NULL)
- lvl2 | int | null | default (NULL)
- lvl3 | int | null | default (NULL)
- lvl4 | int | null | default (NULL)
- lvl5 | int | null | default (NULL)
- lvl6 | int | null | default (NULL)
- lvl7 | int | null | default (NULL)
- lvl8 | int | null | default (NULL)
- lvl9 | int | null | default (NULL)
- lvl10 | int | null | default (NULL)
- id_plan_list | int | not null

## dbo.plan_list
- id | int | not null | PK
- naim | varchar(150) | not null
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- filterlinked | tinyint | not null | default ((0))
- id_owner | int | not null
- id_module | int | null | default (NULL)

## dbo.plan_privileges
- id | int | not null | PK
- is_view | tinyint | not null | default ((0))
- is_edit | tinyint | not null | default ((0))
- is_delete | tinyint | not null | default ((0))
- id_user | int | null | default (NULL)
- id_group | int | null | default (NULL)
- id_plan_list | int | not null

## dbo.plan_structure
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- id_prop_lnk | int | null | default (NULL)
- id_elem_fltr | int | null | default (NULL)
- id_elem_lnk | int | null | default (NULL)
- group_level | int | null | default (NULL)
- id_elem_fltr_main | int | null | default (NULL)
- list_val | text | null | default (NULL)
- iskluch_filter | tinyint | null | default ((0))
- num_val1 | float | null | default (NULL)
- num_val2 | float | null | default (NULL)
- id_elemelem | int | null | default (NULL)
- id_elemprop | int | null | default (NULL)
- id_elemelem1 | int | null | default (NULL)
- id_elemelem2 | int | null | default (NULL)
- id_plan_list | int | not null

## dbo.plan_tree
- id | int | not null | PK
- naim | varchar(150) | null | default (NULL)
- ord_num | int | not null
- id_owner | int | null | default (NULL)
- id_module | int | null | default (NULL)
- id_plan_list | int | null | default (NULL)
- id_plan_tree | int | null | default (NULL)

## dbo.plan_val
- id | int | not null | PK
- pokazatel | int | not null
- plan_val | float | null | default (NULL)
- base_val | float | null | default (NULL)
- correct_perc | float | null | default (NULL)
- correct_val | float | null | default (NULL)
- lvl1 | int | null | default (NULL)
- lvl2 | int | null | default (NULL)
- lvl3 | int | null | default (NULL)
- lvl4 | int | null | default (NULL)
- lvl5 | int | null | default (NULL)
- lvl6 | int | null | default (NULL)
- lvl7 | int | null | default (NULL)
- lvl8 | int | null | default (NULL)
- lvl9 | int | null | default (NULL)
- lvl10 | int | null | default (NULL)
- is_delete | tinyint | not null | default ((0))
- id_plan_list | int | not null

## dbo.podrazd
- id | int | not null | PK
- kod_podrazd | varchar(50) | not null
- naim | varchar(150) | not null

## dbo.points_colors
- id | int | not null | PK
- num_val1 | float | null | default (NULL)
- num_val2 | float | null | default (NULL)
- color_sector | tinyint | not null
- id_operat | tinyint | null | default (NULL)
- id_points_sectors | int | not null

## dbo.points_comm_view
- id | int | not null | PK
- dt_from | datetime | not null
- id_owner | int | not null
- id_points_sectors | int | not null

## dbo.points_elems
- id | int | not null | PK
- naim | varchar(50) | not null
- showintooltip | tinyint | not null | default ((0))
- fact_val | float | null | default (NULL)
- id_rep_tmplt | int | null | default (NULL)
- id_points_sectors | int | not null

## dbo.points_formula
- id | int | not null | PK
- num_val1 | float | null | default (NULL)
- operat | varchar(2) | null | default (NULL)
- id_points_sectors | int | not null
- id_points_elems | int | null | default (NULL)

## dbo.points_inform_sett
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- tip_setting | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- id_points_list | int | not null

## dbo.points_list
- id | int | not null | PK
- naim | varchar(150) | not null
- id_owner | int | not null
- size_point | tinyint | not null
- comm | varchar(250) | null | default (NULL)
- view_point | tinyint | null | default (NULL)
- w_point | smallint | null | default (NULL)
- h_point | smallint | null | default (NULL)

## dbo.points_privileges
- id | int | not null | PK
- is_view | tinyint | not null | default ((0))
- id_user | int | null | default (NULL)
- id_group | int | null | default (NULL)
- id_points_list | int | not null

## dbo.points_sector_comm
- id | int | not null | PK
- dt_from | datetime | not null
- id_owner | int | not null
- msg | varchar(255) | not null
- id_points_sectors | int | not null

## dbo.points_sectors
- id | int | not null | PK
- comm | varchar(250) | null | default (NULL)
- naim | varchar(100) | not null
- num_sector | smallint | null | default (NULL)
- show_decimal | tinyint | not null | default ((0))
- num_digit | tinyint | null | default (NULL)
- id_points_list | int | not null

## dbo.points_settings
- id | int | not null | PK
- id_owner | int | not null
- tip_setting | int | not null
- int_val | int | not null

## dbo.points_tree
- id | int | not null | PK
- ord_num | int | not null
- id_owner | int | not null
- naim | varchar(150) | null | default (NULL)
- id_points_list | int | null | default (NULL)
- id_points_tree | int | null | default (NULL)

## dbo.postavsh
- id | int | not null | PK
- naim | varchar(100) | not null
- kod_post | varchar(50) | null | default (NULL)
- kod_post1 | varchar(50) | null | default (NULL)

## dbo.prod_zakaz_doc
- id | int | not null | PK
- docnum_zak | varchar(100) | not null
- docdate_zak | datetime | not null
- docnum | varchar(100) | null | default (NULL)
- docdate | datetime | null | default (NULL)
- dt_month | tinyint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- dt_year | smallint | null | default (NULL)
- priznak_del | tinyint | null | default ((0))
- in_zak_postav | tinyint | null | default ((0))
- id_kontrag_dogovor | int | null | default (NULL)
- id_klient | int | not null
- id_doc_response | int | null | default (NULL)
- id_podrazd | int | null | default (NULL)
- id_sklad | int | null | default (NULL)
- id_prodagi_tip | int | null | default (NULL)
- id_infsys | int | not null
- id_urlico | int | null | default (NULL)

## dbo.prod_zakaz_str
- id | int | not null | PK
- kol | float | not null
- cena | decimal(20,4) | null | default (NULL)
- sebest | decimal(20,4) | null | default (NULL)
- koff | float | null | default (NULL)
- vozvrat | tinyint | null | default ((0))
- strnum | int | not null
- priznak_del | tinyint | null | default ((0))
- cena2 | decimal(20,4) | null | default (NULL)
- id_prod_zakaz_doc | int | not null
- id_ei | int | null | default (NULL)
- id_klient | int | not null
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_specificat | int | null | default (NULL)

## dbo.prodagi_doc
- id | int | not null | PK
- docnum | varchar(100) | not null
- docdate | datetime | not null
- dt_month | tinyint | null | default (NULL)
- dt_year | smallint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- priznak_del | tinyint | null | default ((0))
- iskluchrasch | tinyint | null | default ((0))
- id_kontrag_dogovor | int | null | default (NULL)
- id_klient | int | not null
- id_doc_response | int | null | default (NULL)
- id_podrazd | int | null | default (NULL)
- id_prodagi_tip | int | not null
- id_infsys | int | not null
- id_urlico | int | null | default (NULL)

## dbo.prodagi_str
- id | int | not null | PK
- strnum | int | not null
- kol | float | not null
- cena | decimal(20,4) | not null
- sebest | decimal(20,4) | null | default (NULL)
- koff | float | null | default (NULL)
- vozvrat | tinyint | null | default ((0))
- docnum_zak | varchar(100) | null | default (NULL)
- docdate_zak | datetime | null | default (NULL)
- priznak_del | tinyint | null | default ((0))
- cena2 | decimal(20,4) | null | default (NULL)
- sebest2 | decimal(20,4) | null | default (NULL)
- date_parttov | datetime | null | default (NULL)
- num_parttov | varchar(50) | null | default (NULL)
- id_prodagi_doc | int | not null
- id_ei | int | not null
- id_klient | int | not null
- id_doc_response | int | null | default (NULL)
- id_postavsh | int | null | default (NULL)
- id_sklad | int | null | default (NULL)
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_specificat | int | null | default (NULL)

## dbo.prodagi_tip
- id | int | not null | PK
- naim | varchar(100) | not null

## dbo.proizvod
- id | int | not null | PK
- naim | varchar(100) | not null
- kod_proizv | varchar(50) | null | default (NULL)

## dbo.sebest_dop
- id | int | not null | PK
- naim | varchar(100) | not null

## dbo.sebest_val_prod
- id | int | not null | PK
- sebest | decimal(20,4) | null | default (NULL)
- id_sebest_dop | int | not null
- id_prodagi_str | int | not null

## dbo.sklad
- id | int | not null | PK
- naim | varchar(100) | not null
- kod_infsys | varchar(70) | null | default (NULL)
- id_sklad | int | null | default (NULL)
- felem209 | int | null | default (NULL)
- id_infsys | int | not null

## dbo.sklad_move_doc
- id | int | not null | PK
- dt_month | tinyint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- dt_year | smallint | null | default (NULL)
- docdate_sklad | datetime | not null
- docnum_sklad | varchar(100) | not null
- id_prodagi_tip | int | not null
- id_infsys | int | not null
- id_urlico | int | null | default (NULL)

## dbo.sklad_move_str
- id | int | not null | PK
- kol | float | not null
- cena | decimal(20,4) | not null
- koff | float | null | default (NULL)
- cena2 | decimal(20,4) | null | default (NULL)
- strnum_sklad | int | not null
- date_parttov | datetime | null | default (NULL)
- num_parttov | varchar(50) | null | default (NULL)
- id_sklad_move_doc | int | not null
- id_ei | int | not null
- id_doc_zakup | int | null | default (NULL)
- id_postavsh | int | null | default (NULL)
- id_sklad | int | not null
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_specificat | int | null | default (NULL)

## dbo.snabg_rep_source
- id | int | not null | PK
- id_zakup_rep_list_1 | int | not null
- id_zakup_rep_list | int | not null

## dbo.spec_task_tovar
- id | int | not null | PK
- naim | varchar(100) | not null

## dbo.specificat
- id | int | not null | PK
- naim | varchar(150) | not null
- kod_infsys | varchar(100) | null | default (NULL)

## dbo.srok_godn
- id | int | not null | PK
- kol | float | not null
- dt_to | datetime | not null
- id_ei | int | not null
- id_sklad | int | not null
- id_tovar | int | not null

## dbo.ss_comp_elem
- id | int | not null | PK
- id_elemelem | int | null | default (NULL)
- id_elemprop | int | null | default (NULL)
- comp_group | int | not null

## dbo.ss_config
- id | int | not null | PK
- naim | varchar(20) | not null
- id_sys | int | not null

## dbo.ss_crdt
- crdt | datetime | null | default (NULL)
- dbk | varchar(100) | null | default (NULL)

## dbo.ss_days
- dt | datetime | null | default (NULL)
- dt_month | tinyint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- dt_year | int | null | default (NULL)

## dbo.ss_elem
- id | int | not null | PK
- naim_short | varchar(50) | not null
- naim_long | varchar(100) | not null
- naim_tbl | varchar(25) | null | default (NULL)
- id_sys | int | null | default (NULL)
- id_config | int | not null
- sys | tinyint | not null | default ((0))
- id_elemprop_ident | int | null | default (NULL)

## dbo.ss_elem_constraint
- id | int | not null | PK
- constr_naim | varchar(50) | not null
- id_elem | int | not null
- id_link | int | not null
- ref_table_naim | varchar(50) | not null
- ref_field_naim | varchar(50) | not null

## dbo.ss_elem_index
- id | int | not null | PK
- naim | varchar(50) | not null
- id_elem | int | not null
- uniq_index | tinyint | not null | default ((0))

## dbo.ss_elem_index_fields
- id | int | not null | PK
- id_index | int | not null
- id_lnk | int | not null

## dbo.ss_elem_trigger
- id | int | not null | PK
- naim | varchar(50) | not null
- id_elem | int | not null
- text_mysql | text | not null
- text_mssql | text | not null
- text_postgresql | text | null | default (NULL)

## dbo.ss_import_files
- id | int | not null | PK
- id_import_tip | int | not null
- file_path | text | not null
- autoimp | tinyint | not null | default ((0))

## dbo.ss_import_fld
- id | int | not null | PK
- id_import_tip | int | not null
- naim_fld | varchar(50) | not null
- id_elemprop | int | not null
- del_by_fld | tinyint | not null | default ((0))
- del_all | tinyint | not null | default ((0))
- id_elemparent | int | null | default (NULL)
- id_elemelem | int | null | default (NULL)

## dbo.ss_import_start
- id | int | not null | PK
- id_import_tip | int | not null
- id_user | int | not null
- date_start | datetime | null | default (NULL)

## dbo.ss_import_tip
- id | int | not null | PK
- naim | varchar(50) | null | default (NULL)
- id_config | int | not null
- id_file_tip | int | not null
- id_sys | int | null | default (NULL)
- id_codepage | int | null | default (NULL)

## dbo.ss_init_elemprop
- id | int | not null | PK
- id_link_elemprop | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- dec_val | decimal(10,0) | null | default (NULL)
- date_val | datetime | null | default (NULL)
- str_val | text | null | default (NULL)
- bool_val | tinyint | null | default (NULL)

## dbo.ss_init_elemprop_cond
- id | int | not null | PK
- id_link_elemprop_cond | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- dec_val | decimal(10,0) | null | default (NULL)
- date_val | datetime | null | default (NULL)
- str_val | text | null | default (NULL)
- bool_val | tinyint | null | default (NULL)
- id_operation | int | not null
- id_val | int | not null
- cond_group | smallint | not null

## dbo.ss_interface_settings
- id | int | not null | PK
- id_user | int | not null
- id_sys_tip | int | not null
- txt | text | null | default (NULL)
- color | int | null | default (NULL)

## dbo.ss_inventory
- naim | varchar(20) | not null
- dt_cr | datetime | not null

## dbo.ss_links_elemprop
- id | int | not null | PK
- id_elem | int | not null
- id_prop_lnk | int | null | default (NULL)
- id_elem_lnk | int | null | default (NULL)
- req_link | tinyint | not null | default ((0))
- uniq_link | tinyint | not null | default ((0))
- autoinc_link | tinyint | not null | default ((0))
- num_link | tinyint | not null | default ((0))
- naim_link | varchar(50) | null | default (NULL)

## dbo.ss_locales
- id | int | not null | PK
- naim_short | varchar(200) | not null
- naim_long | varchar(250) | null | default (NULL)
- id_tip | int | not null
- id_obj | int | not null
- id_locale | int | not null

## dbo.ss_module_elem
- id | int | not null | PK
- id_elem | int | not null
- id_module | int | not null
- select_elem | tinyint | not null | default ((0))
- update_elem | tinyint | not null | default ((0))

## dbo.ss_module_view_columns
- id | int | not null | PK
- id_module_view_settings | int | not null
- id_user | int | not null
- is_visible | tinyint | not null | default ((0))

## dbo.ss_module_view_settings
- id | int | not null | PK
- id_module | int | not null
- tip | int | not null
- id_group | int | null | default (NULL)
- id_user | int | null | default (NULL)
- col_view | tinyint | not null
- row_view | tinyint | not null
- width_view | tinyint | not null | default ((1))
- height_view | tinyint | not null | default ((1))
- id_elemelem | int | null | default (NULL)
- id_elemprop | int | null | default (NULL)
- id_elemelem1 | int | null | default (NULL)
- id_elemelem2 | int | null | default (NULL)
- title | varchar(50) | null | default (NULL)
- bkgrd_lbl | int | not null
- dop_filter | tinyint | not null | default ((0))
- bold_lbl | tinyint | not null | default ((0))
- subspr_tbl | tinyint | not null | default ((0))

## dbo.ss_modules
- id | int | not null | PK
- module_name | varchar(100) | not null
- kod | varchar(20) | not null
- klass | text | not null
- sys_module | tinyint | not null | default ((0))
- singleton | tinyint | not null | default ((0))
- sub_module | tinyint | not null | default ((0))
- sys_stat | bigint | null | default (NULL)
- scr | varbinary(-1) | null | default (NULL)
- module_path | varchar(250) | null | default (NULL)
- have_filters | tinyint | not null | default ((0))

## dbo.ss_operations
- id | int | not null | PK
- naim | varchar(10) | null | default (NULL)
- comm | varchar(100) | null | default (NULL)

## dbo.ss_priv_obj
- id | int | not null | PK
- id_tip | int | not null
- naim | varchar(50) | not null
- naim_prog | varchar(50) | not null
- naim_1 | varchar(50) | null | default (NULL)
- not_use | tinyint | not null | default ((0))
- dec_len | int | null | default (NULL)

## dbo.ss_priv_obj_mod
- id | int | not null | PK
- id_obj | int | not null
- id_obj_1 | int | null | default (NULL)
- id_module | int | not null
- ord_num | int | null | default (NULL)

## dbo.ss_priv_obj_tip
- id | int | not null | PK
- naim | varchar(50) | not null
- naim_prog | varchar(50) | not null

## dbo.ss_priv_obj_usrgr
- id | int | not null | PK
- id_group | int | null | default (NULL)
- id_user | int | null | default (NULL)
- id_lnk_obj_mod | int | not null
- select_priv | tinyint | not null | default ((0))

## dbo.ss_privileges_modules
- id | int | not null | PK
- id_module | int | not null
- id_group | int | null | default (NULL)
- id_user | int | null | default (NULL)
- select_priv | tinyint | not null | default ((0))

## dbo.ss_privileges_pokaz
- id | int | not null | PK
- id_obj | int | not null
- id_group | int | null | default (NULL)
- id_user | int | null | default (NULL)
- select_priv | tinyint | not null | default ((0))

## dbo.ss_privileges_struct
- id | int | not null | PK
- id_group | int | null | default (NULL)
- id_user | int | null | default (NULL)
- id_elem | int | not null
- id_elemelem | int | null | default (NULL)
- id_elemprop | int | null | default (NULL)
- select_priv | tinyint | not null | default ((0))
- insert_priv | tinyint | not null | default ((0))
- update_priv | tinyint | not null | default ((0))
- delete_priv | tinyint | not null | default ((0))
- iskluch_val | tinyint | not null | default ((0))

## dbo.ss_privileges_struct_val
- id | int | not null | PK
- id_privilege | int | not null
- id_val | int | not null

## dbo.ss_prop
- id | int | not null | PK
- naim_short | varchar(60) | not null
- naim_long | varchar(100) | not null
- id_proptip | int | not null
- prop_len | int | null | default (NULL)
- naim_fld | varchar(20) | null | default (NULL)
- id_sys | int | null | default (NULL)
- id_config | int | not null
- sys | tinyint | not null | default ((0))

## dbo.ss_prop_tip
- id | int | not null | PK
- naim | varchar(30) | not null
- tip_sql | varchar(20) | not null

## dbo.ss_rep_tmplt
- id | int | not null | PK
- id_user | int | null | default (NULL)
- id_module | int | not null
- naim | varchar(250) | not null
- start_period | tinyint | null | default (NULL)
- end_period | tinyint | null | default (NULL)
- end_interval | tinyint | null | default (NULL)
- start_period_brt | tinyint | null | default (NULL)
- end_period_brt | tinyint | null | default (NULL)
- end_interval_brt | tinyint | null | default (NULL)
- interval1 | tinyint | null | default (NULL)
- interval2 | tinyint | null | default (NULL)
- interval3 | tinyint | null | default (NULL)
- kol_interval2 | int | null | default (NULL)
- kol_interval3 | int | null | default (NULL)
- kol_interval_save | int | null | default (NULL)
- is_manual | tinyint | not null | default ((0))
- upd_to_last | tinyint | not null | default ((0))
- is_fix_period | tinyint | not null | default ((0))
- is_fix_period_brt | tinyint | not null | default ((0))
- days_fix_period | int | null | default (NULL)
- days_fix_period_brt | int | null | default (NULL)
- upd_period | tinyint | null | default (NULL)
- nakopit_period | tinyint | not null | default ((0))
- save_intervals | tinyint | not null | default ((0))

## dbo.ss_rep_tmplt_cell
- id | int | not null | PK
- id_rep_tmplt | int | not null
- lvl1 | int | null | default (NULL)
- lvl2 | int | null | default (NULL)
- lvl3 | int | null | default (NULL)
- lvl4 | int | null | default (NULL)
- lvl5 | int | null | default (NULL)
- lvl6 | int | null | default (NULL)
- lvl7 | int | null | default (NULL)
- lvl8 | int | null | default (NULL)
- lvl9 | int | null | default (NULL)
- lvl10 | int | null | default (NULL)
- col_naim | varchar(80) | null | default (NULL)

## dbo.ss_rep_tmplt_dop_cols
- id | int | not null | PK
- id_rep_tmplt | int | not null
- id_elemprop | int | not null | default ((0))
- id_elemelem | int | null | default (NULL)
- id_elemelem1 | int | null | default (NULL)
- id_elemelem2 | int | null | default (NULL)
- id_link_dop | int | null | default (NULL)
- id_link_elel_dop | int | null | default (NULL)

## dbo.ss_rep_tmplt_favorite
- id | int | not null | PK
- id_user | int | not null
- id_rep_tmplt | int | not null

## dbo.ss_rep_tmplt_fltr
- id | int | not null | PK
- id_rep_tmplt | int | not null
- group_level | int | null | default (NULL)
- id_elemprop | int | not null | default ((0))
- id_elemelem | int | null | default (NULL)
- id_elemelem1 | int | null | default (NULL)
- id_elemelem2 | int | null | default (NULL)
- id_prop_lnk | int | null | default (NULL)
- id_elem_lnk | int | null | default (NULL)
- id_elem_fltr | int | null | default (NULL)
- id_elem_fltr_main | int | null | default (NULL)
- list_val | text | null | default (NULL)
- iskluch_fltr | tinyint | null | default (NULL)
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- num_val1 | float | null | default (NULL)
- num_val2 | float | null | default (NULL)

## dbo.ss_rep_tmplt_informer_sett
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- tip_setting | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- id_rep_tmplt | int | not null

## dbo.ss_rep_tmplt_settings
- id | int | not null | PK
- id_rep_tmplt | int | not null
- id_tip | int | not null
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- num_val1 | int | null | default (NULL)
- num_val2 | int | null | default (NULL)
- num_val3 | int | null | default (NULL)
- num_val4 | int | null | default (NULL)
- txt_val | varchar(30) | null | default (NULL)

## dbo.ss_reqst_tree
- id | int | not null | PK
- naim | varchar(150) | null | default (NULL)
- ord_num | int | not null
- sys | tinyint | not null | default ((0))
- id_request_tree | int | null | default (NULL)
- id_request | int | null | default (NULL)

## dbo.ss_request
- id | int | not null | PK
- naim | varchar(150) | null | default (NULL)
- sys | tinyint | not null | default ((0))

## dbo.ss_request_data
- id | int | not null | PK
- id_request | int | not null
- id_elem_doc | int | null | default (NULL)
- id_elem_str | int | null | default (NULL)
- id_elem_kl | int | null | default (NULL)
- id_elem_tov | int | null | default (NULL)
- id_pr_doc_date | int | null | default (NULL)
- id_pr_doc_num | int | null | default (NULL)
- id_pr_cena | int | null | default (NULL)
- id_pr_kol | int | null | default (NULL)
- id_pr_koff | int | null | default (NULL)
- id_pr_sebest | int | null | default (NULL)
- id_pr_vozvr | int | null | default (NULL)
- id_pr_strnum | int | null | default (NULL)

## dbo.ss_request_priv
- id | int | not null | PK
- is_view | tinyint | not null | default ((0))
- id_user | int | null | default (NULL)
- id_group | int | null | default (NULL)
- id_request | int | not null

## dbo.ss_robot
- id | int | not null | PK
- id_module | int | not null
- naim | varchar(250) | not null
- id_robot_prnt | int | null | default (NULL)

## dbo.ss_robot_settings
- id | int | not null | PK
- id_robot | int | not null
- id_tip | int | not null
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- num_val1 | int | null | default (NULL)
- num_val2 | int | null | default (NULL)
- num_val3 | int | null | default (NULL)
- num_val4 | int | null | default (NULL)
- num_val5 | int | null | default (NULL)
- list_val | text | null | default (NULL)

## dbo.ss_sessions
- id | int | not null | PK
- id_user | int | not null
- enter_dt | datetime | not null

## dbo.ss_sessions_mod
- id_session | int | not null
- id_module | int | not null
- kol_module | int | not null

## dbo.ss_stat_module_enter
- id | int | not null | PK
- id_user | int | not null
- id_module | int | not null
- dt_enter | datetime | not null
- is_success | tinyint | not null | default ((0))

## dbo.ss_syssettings
- id | int | not null | PK
- id_tip | int | not null
- int_value | int | null | default (NULL)
- float_value | float | null | default (NULL)
- date_value | datetime | null | default (NULL)
- list_val | text | null | default (NULL)
- comm | varchar(100) | null | default (NULL)

## dbo.ss_syssettings_tip
- id | int | not null | PK
- naim | varchar(50) | not null
- comm | varchar(100) | null | default (NULL)

## dbo.ss_user_settings
- id | int | not null | PK
- id_tip | int | not null
- id_user | int | null | default (NULL)
- int_value | int | null | default (NULL)
- txt_val | text | null | default (NULL)

## dbo.ss_users
- id | int | not null | PK
- naim_short | varchar(50) | not null
- naim_long | varchar(100) | null | default (NULL)
- psw | text | not null
- id_group | int | null | default (NULL)
- change_psw | tinyint | not null | default ((0))
- sex | tinyint | null | default (NULL)
- naim_help | varchar(20) | null | default (NULL)
- birthday | datetime | null | default (NULL)
- id_activity | int | null | default (NULL)
- font_size | int | null | default (NULL)
- local_intf | tinyint | not null | default ((0))
- dt_curr_tmplt | datetime | null | default (NULL)
- autoorder_path | text | null | default (NULL)
- autoorder_ext | tinyint | null | default (NULL)
- zakaz1c_path | text | null | default (NULL)
- autoinformer_path | text | null | default (NULL)
- autoinformer_ext | tinyint | null | default (NULL)

## dbo.ss_usrgroups
- id | int | not null | PK
- naim | varchar(100) | not null
- id_sys | int | null | default (NULL)

## dbo.ss_workplaces
- id | int | not null | PK
- id_user | int | not null
- id_module | int | null | default (NULL)
- naim | varchar(100) | null | default (NULL)
- is_expanded | tinyint | not null | default ((0))
- ord_num | int | not null
- parent_ord_num | int | null | default (NULL)
- col_wp | int | null | default (NULL)
- row_wp | int | null | default (NULL)
- icon_path | text | null | default (NULL)

## dbo.ss_workplaces_adm
- id | int | not null | PK
- id_user | int | not null
- id_module | int | null | default (NULL)
- naim | varchar(100) | null | default (NULL)
- is_expanded | tinyint | not null | default ((0))
- ord_num | int | not null
- parent_ord_num | int | null | default (NULL)
- col_wp | int | null | default (NULL)
- row_wp | int | null | default (NULL)
- icon_path | text | null | default (NULL)

## dbo.status_tov
- id | int | not null | PK
- naim | varchar(30) | not null

## dbo.status_zapas
- id | int | not null | PK
- naim | varchar(30) | not null

## dbo.svodn_tmplt_inform_sett
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- tip_setting | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- id_svodn_tmplt_list | int | not null

## dbo.svodn_tmplt_list
- id | int | not null | PK
- id_owner | int | not null
- naim | varchar(150) | not null
- first_tmplt | tinyint | not null | default ((0))

## dbo.svodn_tmplt_privileges
- id | int | not null | PK
- is_view | tinyint | not null | default ((0))
- id_user | int | null | default (NULL)
- id_group | int | null | default (NULL)
- id_svodn_tmplt_list | int | not null

## dbo.svodn_tmplt_settings
- id | int | not null | PK
- ord_num | int | not null
- id_rep_tmplt | int | not null
- pref_column | varchar(20) | null | default (NULL)
- id_svodn_tmplt_list | int | not null

## dbo.svodn_tmplt_tree
- id | int | not null | PK
- ord_num | int | null | default (NULL)
- id_owner | int | null | default (NULL)
- naim | varchar(150) | null | default (NULL)
- id_svodn_tmplt_tree | int | null | default (NULL)
- id_svodn_tmplt_list | int | null | default (NULL)

## dbo.tenden_colors
- id | int | not null | PK
- num_val1 | float | null | default (NULL)
- num_val2 | float | null | default (NULL)
- id_operat | tinyint | null | default (NULL)
- color_tenden | tinyint | not null
- id_tenden_list | int | not null

## dbo.tenden_comm
- id | int | not null | PK
- dt_from | datetime | not null
- id_owner | int | not null
- msg | varchar(255) | not null
- id_tenden_list | int | not null

## dbo.tenden_comm_view
- id | int | not null | PK
- dt_from | datetime | not null
- id_owner | int | not null
- id_tenden_list | int | not null

## dbo.tenden_inform_sett
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- tip_setting | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- id_tenden_list | int | not null

## dbo.tenden_list
- id | int | not null | PK
- with_prognoz | tinyint | not null | default ((0))
- id_owner | int | not null
- comm | varchar(250) | null | default (NULL)
- id_rep_tmplt | int | null | default (NULL)
- show_decimal | tinyint | not null | default ((0))
- num_digit | tinyint | null | default (NULL)
- view_tenden | tinyint | null | default (NULL)
- h_tenden | smallint | null | default (NULL)
- naim | varchar(150) | not null
- size_tenden | tinyint | not null
- w_tenden | smallint | null | default (NULL)
- show_values | tinyint | not null | default ((0))
- tip_informer | tinyint | null | default (NULL)
- show_trend | tinyint | not null | default ((0))
- plan_val | float | null | default (NULL)
- show_koff | tinyint | not null | default ((0))
- show_plan_vipoln | tinyint | not null | default ((0))
- trend_no_last_interv | tinyint | not null | default ((0))
- show_trend_dop | tinyint | not null | default ((0))
- show_koff_dop | tinyint | not null | default ((0))
- trend_dop_interval | smallint | null | default (NULL)
- plan_vipoln_backward | tinyint | not null | default ((0))

## dbo.tenden_privileges
- id | int | not null | PK
- is_view | tinyint | not null | default ((0))
- id_user | int | null | default (NULL)
- id_group | int | null | default (NULL)
- id_tenden_list | int | not null

## dbo.tenden_settings
- id | int | not null | PK
- id_owner | int | not null
- tip_setting | int | not null
- int_val | int | not null

## dbo.tenden_tree
- id | int | not null | PK
- ord_num | int | null | default (NULL)
- id_owner | int | not null
- naim | varchar(150) | null | default (NULL)
- id_tenden_list | int | null | default (NULL)
- id_tenden_tree | int | null | default (NULL)

## dbo.tovar
- id | int | not null | PK
- naim | varchar(200) | not null
- kod_infsys | varchar(100) | not null
- iskluchrasch | tinyint | null | default ((0))
- ves | float | null | default (NULL)
- articul | varchar(50) | null | default (NULL)
- kod_infsys1 | varchar(100) | null | default (NULL)
- naim_long | varchar(200) | null | default (NULL)
- dt_status_tovar | datetime | null | default (NULL)
- dt_status_zapas | datetime | null | default (NULL)
- fprop389 | float | null | default (NULL)
- dt_status_zapas_osn | datetime | null | default (NULL)
- dt_status_tovar_osn | datetime | null | default (NULL)
- id_ei | int | null | default (NULL)
- felem22 | int | null | default (NULL)
- felem169 | int | null | default (NULL)
- felem168 | int | null | default (NULL)
- id_ei_1 | int | null | default (NULL)
- id_tovar_cat | int | null | default (NULL)
- id_tovar | int | null | default (NULL)
- id_doc_zakup | int | null | default (NULL)
- id_postavsh | int | null | default (NULL)
- id_proizvod | int | null | default (NULL)
- id_spec_task_tovar | int | null | default (NULL)
- id_status_zapas | int | null | default (NULL)
- id_status_zapas_1 | int | null | default (NULL)
- id_status_tov | int | null | default (NULL)
- id_status_tov_1 | int | null | default (NULL)
- felem23 | int | null | default (NULL)
- felem17 | int | null | default (NULL)
- felem19 | int | null | default (NULL)
- felem20 | int | null | default (NULL)
- felem21 | int | null | default (NULL)
- felem35 | int | null | default (NULL)
- felem166 | int | null | default (NULL)
- felem167 | int | null | default (NULL)
- id_infsys | int | null | default (NULL)
- id_nelikvid_prizn | int | null | default (NULL)
- id_nelikvid_prizn_1 | int | null | default (NULL)

## dbo.tovar_cat
- id | int | not null | PK
- naim | varchar(100) | not null

## dbo.tovar_kompl
- id | int | not null | PK
- kol | float | null | default (NULL)
- id_ei | int | null | default (NULL)
- id_tovar_1 | int | not null
- id_tovar | int | not null

## dbo.tovar_sklad_link
- id | int | not null | PK
- id_tovar_sklad_priznak1 | int | not null
- id_sklad | int | not null
- id_tovar | int | not null
- id_infsys | int | not null

## dbo.tovar_sklad_max_zapas
- id | int | not null | PK
- kol | float | null | default (NULL)
- id_ei | int | null | default (NULL)
- id_sklad | int | not null
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_infsys | int | null | default (NULL)
- id_specificat | int | null | default (NULL)

## dbo.tovar_sklad_min_zakaz
- id | int | not null | PK
- kol | float | null | default (NULL)
- ostatok_ei1 | float | null | default (NULL)
- ostatok_ei2 | float | null | default (NULL)
- ostatok_ei3 | float | null | default (NULL)
- id_ei | int | null | default (NULL)
- id_sklad | int | not null
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_infsys | int | null | default (NULL)
- id_specificat | int | null | default (NULL)

## dbo.tovar_sklad_priznak1
- id | int | not null | PK
- naim | varchar(100) | not null

## dbo.tovar_specific
- id | int | not null | PK
- naim | varchar(350) | null | default (NULL)
- dt_status_tovar | datetime | null | default (NULL)
- dt_status_zapas | datetime | null | default (NULL)
- id_status_zapas | int | null | default (NULL)
- id_status_tov | int | null | default (NULL)
- id_tovar | int | not null
- id_nelikvid_prizn | int | null | default (NULL)
- id_specificat | int | null | default (NULL)

## dbo.tovar_status_tov
- id | int | not null | PK
- id_sklad | int | not null
- id_status_tov | int | not null
- id_tovar | int | not null

## dbo.tovar_status_tov_osn
- id | int | not null | PK
- id_sklad | int | not null
- id_status_tov | int | not null
- id_tovar | int | not null

## dbo.tovar_status_zapas
- id | int | not null | PK
- id_sklad | int | not null
- id_status_zapas | int | not null
- id_tovar | int | not null
- id_nelikvid_prizn | int | null | default (NULL)

## dbo.tovar_status_zapas_osn
- id | int | not null | PK
- id_sklad | int | not null
- id_status_zapas | int | not null
- id_tovar | int | not null
- id_nelikvid_prizn | int | null | default (NULL)

## dbo.tovarspec_status_tov
- id | int | not null | PK
- id_sklad | int | not null
- id_status_tov | int | not null
- id_tovar_specific | int | not null

## dbo.tovarspec_status_zapas
- id | int | not null | PK
- id_sklad | int | not null
- id_status_zapas | int | not null
- id_tovar_specific | int | not null
- id_nelikvid_prizn | int | null | default (NULL)

## dbo.tovspec_status_tov_osn
- id | int | not null | PK
- id_sklad | int | not null
- id_status_tov | int | not null
- id_tovar_specific | int | not null

## dbo.tovspec_status_zps_osn
- id | int | not null | PK
- id_sklad | int | not null
- id_status_zapas | int | not null
- id_tovar_specific | int | not null
- id_nelikvid_prizn | int | null | default (NULL)

## dbo.urlico
- id | int | not null | PK
- naim | varchar(150) | not null
- kod_infsys | varchar(70) | null | default (NULL)

## dbo.zakaz_postav_doc
- id | int | not null | PK
- zakaznum | int | not null
- docdate_zak_postavsh | datetime | not null
- in_road | tinyint | not null | default ((0))
- is_closed | tinyint | not null | default ((0))
- comm | varchar(150) | null | default (NULL)
- date_prixod | datetime | null | default (NULL)
- zakaznum_1с | varchar(100) | null | default (NULL)
- summ_doc | decimal(20,4) | null | default (NULL)
- is_internal | tinyint | null | default ((0))
- dt_month | tinyint | null | default (NULL)
- dt_day | tinyint | null | default (NULL)
- dt_week | tinyint | null | default (NULL)
- dt_quarter | tinyint | null | default (NULL)
- dt_year | smallint | null | default (NULL)
- id_currency | int | null | default (NULL)
- id_postavsh | int | null | default (NULL)
- id_sklad | int | null | default (NULL)
- id_zakaz_postav_stat | int | null | default (NULL)
- id_prodagi_tip | int | null | default (NULL)
- id_infsys | int | not null
- id_urlico | int | null | default (NULL)

## dbo.zakaz_postav_stat
- id | int | not null | PK
- naim_stat | varchar(60) | not null

## dbo.zakaz_postav_str
- id | int | not null | PK
- kol | float | not null
- cena | decimal(20,4) | null | default (NULL)
- strnum | int | not null
- summ_doc | decimal(20,4) | null | default (NULL)
- is_closed | tinyint | not null | default ((0))
- comm | varchar(150) | null | default (NULL)
- kol_ei2 | float | null | default (NULL)
- kol_ei3 | float | null | default (NULL)
- kol_ei1 | float | null | default (NULL)
- cena_ei1 | decimal(20,4) | null | default (NULL)
- cena_ei2 | decimal(20,4) | null | default (NULL)
- cena_ei3 | decimal(20,4) | null | default (NULL)
- summ_ei1 | decimal(20,4) | null | default (NULL)
- summ_ei2 | decimal(20,4) | null | default (NULL)
- summ_ei3 | decimal(20,4) | null | default (NULL)
- summ_ei4 | decimal(20,4) | null | default (NULL)
- cena_ei4 | decimal(20,4) | null | default (NULL)
- summ_ei5 | decimal(20,4) | null | default (NULL)
- cena_ei5 | decimal(20,4) | null | default (NULL)
- kol_ei4 | float | null | default (NULL)
- kol_ei5 | float | null | default (NULL)
- id_zakaz_postav_doc | int | not null
- id_ei | int | null | default (NULL)
- id_tovar | int | not null
- id_tovar_specific | int | null | default (NULL)
- id_specificat | int | null | default (NULL)

## dbo.zakup_rep_autoorder_sett
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- tip_setting | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- id_rep_tmplt | int | null | default (NULL)
- id_zakup_rep_list | int | null | default (NULL)

## dbo.zakup_rep_def_col
- id | int | not null | PK
- id_user | int | not null
- colnum | int | not null
- is_selected | tinyint | not null | default ((0))
- id_zakup_rep_list | int | not null

## dbo.zakup_rep_dop_col
- id | int | not null | PK
- id_elemprop | int | not null
- id_elemelem | int | null | default (NULL)
- id_elemelem1 | int | null | default (NULL)
- id_elemelem2 | int | null | default (NULL)
- id_link_dop | int | null | default (NULL)
- id_link_elel_dop | int | null | default (NULL)
- id_zakup_rep_list | int | not null

## dbo.zakup_rep_list
- id | int | not null | PK
- filterlinked | tinyint | not null | default ((0))
- id_owner | int | not null
- naim | varchar(150) | not null
- tip_zakup_rep | tinyint | null | default (NULL)
- algoritm_pererasp | tinyint | null | default (NULL)
- norma_pererasp | float | null | default (NULL)
- zero_ost_pererasp | tinyint | not null | default ((0))
- min_ostatok_pererasp | float | null | default (NULL)
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- dt_start | datetime | null | default (NULL)
- defizit_no_otgr | tinyint | not null | default ((0))
- pokazatel | int | null | default (NULL)
- start_period | tinyint | null | default (NULL)
- end_period | tinyint | null | default (NULL)
- end_interval | tinyint | null | default (NULL)
- start_period_brt | tinyint | null | default (NULL)
- interval1 | int | null | default (NULL)
- interval2 | int | null | default (NULL)
- end_period_brt | tinyint | null | default (NULL)
- kol_interval_save | tinyint | null | default (NULL)
- kol_interval2 | tinyint | null | default (NULL)
- show_novinka | tinyint | null | default ((0))
- show_nerasppriem | tinyint | null | default ((0))
- show_neraspotgryz | tinyint | null | default ((0))
- show_ei1 | tinyint | null | default ((0))
- show_ei2 | tinyint | null | default ((0))
- show_ei3 | tinyint | null | default ((0))
- show_ei4 | tinyint | null | default ((0))
- show_ei5 | tinyint | null | default ((0))
- end_interval_brt | tinyint | null | default (NULL)
- is_fix_period | tinyint | null | default ((0))
- is_fix_period_brt | tinyint | null | default ((0))
- days_fix_period_brt | int | null | default (NULL)
- days_fix_period | int | null | default (NULL)

## dbo.zakup_rep_privileges
- id | int | not null | PK
- is_view | tinyint | not null | default ((0))
- is_edit | tinyint | not null | default ((0))
- is_delete | tinyint | not null | default ((0))
- id_user | int | null | default (NULL)
- id_group | int | null | default (NULL)
- id_zakup_rep_list | int | not null

## dbo.zakup_rep_sett
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- tip_setting | int | not null
- int_val | int | null | default (NULL)
- dbl_val | float | null | default (NULL)
- id_zakup_rep_list | int | not null

## dbo.zakup_rep_structure
- id | int | not null | PK
- dt_from | datetime | null | default (NULL)
- dt_to | datetime | null | default (NULL)
- id_prop_lnk | int | null | default (NULL)
- id_elem_fltr | int | null | default (NULL)
- id_elem_lnk | int | null | default (NULL)
- group_level | int | null | default (NULL)
- id_elem_fltr_main | int | null | default (NULL)
- list_val | text | null | default (NULL)
- iskluch_filter | tinyint | null | default ((0))
- num_val1 | float | null | default (NULL)
- num_val2 | float | null | default (NULL)
- id_elemprop | int | null | default (NULL)
- id_elemelem | int | null | default (NULL)
- id_elemelem1 | int | null | default (NULL)
- id_elemelem2 | int | null | default (NULL)
- id_zakup_rep_list | int | not null

## dbo.zakup_rep_tree
- id | int | not null | PK
- ord_num | int | not null
- id_owner | int | not null
- naim | varchar(150) | null | default (NULL)
- id_zakup_rep_list | int | null | default (NULL)
- id_zakup_rep_tree | int | null | default (NULL)

## dbo.zakup_rep_val
- id | int | not null | PK
- pokazatel | int | not null
- lvl1 | int | null | default (NULL)
- lvl2 | int | null | default (NULL)
- lvl3 | int | null | default (NULL)
- lvl4 | int | null | default (NULL)
- lvl5 | int | null | default (NULL)
- lvl6 | int | null | default (NULL)
- lvl7 | int | null | default (NULL)
- lvl8 | int | null | default (NULL)
- lvl9 | int | null | default (NULL)
- lvl10 | int | null | default (NULL)
- zapas_val | float | null | default (NULL)
- srok_days | float | null | default (NULL)
- zapas_days_val | float | null | default (NULL)
- zakaz_val | float | null | default (NULL)
- base_val | float | null | default (NULL)
- correct_perc | float | null | default (NULL)
- correct_zak_perc | float | null | default (NULL)
- correct_zak_val | float | null | default (NULL)
- min_zakaz | float | null | default (NULL)
- brt_service_lvl | float | null | default (NULL)
- koff_season | float | null | default (NULL)
- koff_season2 | float | null | default (NULL)
- koff_season1 | float | null | default (NULL)
- correct_perc2 | float | null | default (NULL)
- correct_perc1 | float | null | default (NULL)
- potrebn_gp | float | null | default (NULL)
- id_zakup_rep_list | int | not null
