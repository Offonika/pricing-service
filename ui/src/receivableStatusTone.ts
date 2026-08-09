export function receivableStatusTone(status: string) {
  const tones: Record<string, string> = {
    new_debt: "blue",
    no_answer: "blue",
    calling: "blue",
    sms_sent: "blue",
    waiting_payment: "light-green",
    promised_payment: "light-green",
    call_back: "yellow",
    remind: "yellow",
    data_quality_error: "yellow",
    intervention_required: "red",
    escalated: "red",
    dispute: "red",
    dispute_check: "red",
    paid: "green",
    closed: "green",
    transfer: "purple",
    not_ours_transfer: "purple",
    on_card_route: "graphite",
    no_phone: "gray",
  };
  return tones[status] || "gray";
}
