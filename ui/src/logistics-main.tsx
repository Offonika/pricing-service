import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import "./App.css";
import { BitrixLogisticsApp } from "./BitrixLogisticsApp";

document.documentElement.dataset.logisticsStarted = "1";

const root = document.getElementById("root");
if (!root) throw new Error("Logistics root element is missing");

createRoot(root).render(
  <StrictMode>
    <BitrixLogisticsApp />
  </StrictMode>
);
