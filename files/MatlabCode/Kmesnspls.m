clc
close all
for i=1:length(AccXmax)
    v=[AccXmax(i,1),AccYmax(i,1),AccZmax(i,1)];
    NormAccmax(i,1) = norm(v);
end
for i=1:length(AccXmin)
    v=[AccXmin(i,1),AccYmin(i,1),AccZmin(i,1)];
    NormAccmin(i,1) = norm(v);
end
for i=1:length(TimeInMax)
    TimeDiff(i,1) = TimeInMax(i)-TimeinMin(i);
end

[ZsNormAccmax,ZsNormAccmaxmean,ZsNormAccmaxstdev] = zscore(NormAccmax);
[ZsPotMag,ZsPotMagmean,ZsPotMagstdev] = zscore(PotMag);
[ZsGyro,ZsGyromean,ZsGyrostdv] = zscore(GyroZmax);
ZsNormAccmax=(ZsNormAccmax);
ZsPotMag=(ZsPotMag);
%normalized = (x-min(NormAccmax))/(max(x)-min(x));
X=[ZsPotMag,ZsNormAccmax,ZsGyro];
[idx,C] = kmeans(X,3,'Start','plus');
figure;
plot(X(idx==1,1),X(idx==1,2),'r.','MarkerSize',12)
hold on
plot(X(idx==2,1),X(idx==2,2),'b.','MarkerSize',12)
plot(X(idx==3,1),X(idx==3,2),'m.','MarkerSize',12)
plot(C(:,1),C(:,2),'kx',...
     'MarkerSize',15,'LineWidth',3)
legend('Cluster 1','Cluster 2','Cluster 3','Centroids',...
       'Location','NW')
title 'Cluster Assignments and Centroids'
hold off
idxNotpot=1;
idxcrack=3;
idxPot=2;
TrainNotPot = [ZsNormAccmax(idx==idxNotpot,1),PotMag(idx==idxNotpot,1),GyroZmax(idx==idxNotpot,1),Long(idx==idxNotpot,1),Lat(idx==idxNotpot,1)];
TrainstdNotPot=[PotMag(idx==idxNotpot,1),NormAccmax(idx==idxNotpot,1)];
Clstype = idxNotpot*ones([length(TrainNotPot),1]);
TrainNotPot = [TrainNotPot,Clstype];
for i=1:length(TrainNotPot)
    type(i,:)={'NotPot'};
end
TrainCrack = [ZsNormAccmax(idx==idxcrack,1),PotMag(idx==idxcrack,1),GyroZmax(idx==idxcrack,1),Long(idx==idxcrack,1),Lat(idx==idxcrack,1)];
TrainstdCrack=[PotMag(idx==idxcrack,1),NormAccmax(idx==idxcrack,1)];
Clstype = idxcrack*ones([length(TrainCrack),1]);
TrainCrack = [TrainCrack,Clstype];
for i=length(TrainNotPot)+1:length(TrainNotPot)+1+length(TrainCrack)
    type(i,:)={'Crack'};
end
TrainPot = [ZsNormAccmax(idx==idxPot,1),PotMag(idx==idxPot,1),GyroZmax(idx==idxPot,1),Long(idx==idxPot,1),Lat(idx==idxPot,1)];
TrainstdPot=[PotMag(idx==idxPot,1),NormAccmax(idx==idxPot,1)];
Clstype = idxPot*ones([length(TrainPot),1]);
TrainPot = [TrainPot,Clstype];
for i=length(TrainNotPot)+length(TrainCrack)+1:length(TrainNotPot)+length(TrainCrack)+1+length(TrainPot)
    type(i,:)={'Pot'};
end
TrainData = [TrainNotPot;TrainCrack;TrainPot];