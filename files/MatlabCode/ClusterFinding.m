function [leni,Xup,T,T1tresh] = ClusterFinding(gg,X,NoOfinst,p)
T=[];
jj=1;
sum=[0,0,0];
ZsRatio = X(:,1);
ZsGbar = X(:,2);
Xup=[];
ii=1;
for i=1:length(X)
    XX(i,:)=[ZsRatio(i),ZsGbar(i)];
    T(jj,1)=(NoOfinst(1)*(NoOfinst(1)-2)/(NoOfinst(1)-1)*2)*(XX(i,:)-gg.mu(p(1),:))*(gg.Sigma(:,:,p(1))^-1)*(XX(i,:)-gg.mu(p(1),:))';
    if(T(jj,1)<= finv(0.95,2,NoOfinst(1)-2))
        T(jj,2)=1;
        sum(1)=sum(1)+T(jj,2);
        index(ii)=i;
        ii=ii+1;
    else
        T(jj,2)=0;
    end
        T(jj,3)=(NoOfinst(2)*(NoOfinst(2)-2)/(NoOfinst(2)-1)*2)*(XX(i,:)-gg.mu(p(2),:))*(gg.Sigma(:,:,p(2))^-1)*(XX(i,:)-gg.mu(p(2),:))';
    if(T(jj,3)<= finv(0.95,2,NoOfinst(2)-2))
        T(jj,4)=1;
        sum(2)=sum(2)+T(jj,3);
        index(ii)=i;
        ii=ii+1;
    else
        T(jj,4)=0;
    end
        T(jj,5)=(NoOfinst(3)*(NoOfinst(3)-2)/(NoOfinst(3)-1)*2)*(XX(i,:)-gg.mu(p(3),:))*(gg.Sigma(:,:,p(1))^-1)*(XX(i,:)-gg.mu(p(3),:))';
    if(T(jj,5)<= finv(0.95,2,NoOfinst(3)-2))
        T(jj,6)=1;
        sum(3)=sum(3)+T(jj,5);
        index(ii)=i;
        ii=ii+1;
    else
        T(jj,6)=0;
    end
    jj=jj+1;
end
Xup=X;
if(~isempty(index))
Xup(index,:)=[];
end
T1tresh = sum(1)+sum(2)+sum(3)/length(index);
leni=length(Xup)+1;
end